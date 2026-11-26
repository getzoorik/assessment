"""How the disk method sizes a pool for a VM: its members, their size and performance, and what they cost a month.

An Azure pool goes on Premium SSD v2 wherever the VM's region and zone offer it, otherwise on Premium SSD; an AWS pool
on equal gp3 volumes. The pool is built beside today's disks, so its members must fit the slots those leave free.
"""

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from zoorik.cloud.keys import AWS, AZURE
from zoorik.disks.facts import PREMIUM_V2
from zoorik.disks.model import AzureLimits, EbsLimits, Hourly, Load
from zoorik.format import GIB, MIB_PER_GIB, SECONDS_PER_MINUTE, half_up
from zoorik.pricing.aws_ebs import GP3_BASE_IOPS, GP3_BASE_MBPS
from zoorik.pricing.azure_disks import TIER_SIZES
from zoorik.pricing.disks import DiskPrices, DiskSpec

# MB/s: 10^6 bytes per second on Azure; on AWS MiB/s, the unit gp3 throughput is set in.
MEGABYTE = {AZURE: 1e6, AWS: float(2**20)}

# The pool holds today's data plus headroom, laid out as anchors and elastics.
HEADROOM_FRACTION = 0.20
HEADROOM_MIN_GIB = 50
ANCHOR_POOL_THRESHOLD_GIB = 512
ANCHOR_COUNT = 2
ANCHOR_CAP_FRACTION = 0.75
ANCHOR_MAX_GIB = 2048
MIN_ELASTICS = 3
MAX_ELASTICS = 6
ELASTIC_FLOOR_V2_GIB = 64
ELASTIC_FLOOR_V1_GIB = 128
ELASTIC_CAP_GIB = 512
ELASTIC_DIVISOR = 16
ELASTIC_FILL_FRACTION = 0.80
EVAC_SHARE = 0.30
EVAC_WINDOW_MINUTES = 120
SMALL_MEMBER_GIB = 8
DEGRADED_ANCHORS = 1
DEGRADED_MAX_ELASTICS = 3
MIN_POOL_SLOTS = 2
MIN_VM_SLOTS = 8
RESERVED_SLOTS = 1
DEMAND_FACTOR = 1.3

# Premium SSD v2 limits, from Azure's documentation.
V2_MIN_IOPS = 3000
V2_MIN_MBPS = 125
V2_MAX_IOPS_PER_GIB = 500
V2_MBPS_PER_IOPS = 0.25
V2_MAX_MBPS = 2000

# Premium SSD sizes, and the provisioned IOPS and MB/s of each, from Azure's documentation (bursting not counted).
PREMIUM_V1 = "Premium_LRS"
V1_TIER_GIB = tuple(size for size, _ in TIER_SIZES)
V1_TIER_PERFORMANCE = {
    4: (120, 25),
    8: (120, 25),
    16: (120, 25),
    32: (120, 25),
    64: (240, 50),
    128: (500, 100),
    256: (1100, 125),
    512: (2300, 150),
    1024: (5000, 200),
    2048: (7500, 250),
    4096: (7500, 250),
    8192: (16000, 500),
    16384: (18000, 750),
    32767: (20000, 900),
}

# How the disk method lays out a new pool on AWS: the hold size, then equal gp3 volumes. A pool is held at most 90%
# full, the high edge of the band every VM starts with.
GP3 = "gp3"
BAND_HIGH = 0.90
URGENT_USED = 0.95
FREE_FLOOR_GIB = 10
BTRFS_OVERHEAD = 0.03
MIN_MEMBERS = 2
GP3_MAX_IOPS = 80000
GP3_IOPS_PER_GIB = 500
GP3_MAX_MBPS = 2000
GP3_MBPS_PER_IOPS = 0.25
GP3_MAX_GIB = 16384

TOO_MUCH_DATA = "too much data for one pool: not estimated"
TOO_BUSY = "too busy for a pool of this size: not estimated"


class NotEstimated(Exception):
    """A pool the disk method doesn't estimate; its text is the reason the reader sees."""


class Overloaded(NotEstimated):
    """A pool that can't carry the load of the disks it replaces."""


@dataclass(frozen=True)
class Demand:
    """What a pool must hold and carry: each mount's used bytes, and the peak of the disks it replaces, in MB/s."""

    used: Tuple[int, ...]
    peak: Load


@dataclass(frozen=True)
class Pool:
    """A pool's size in GB and its list price a month."""

    size_gb: int
    monthly: float


@dataclass(frozen=True)
class AzureRoom:
    """What an Azure pool is laid out within: its size and the data it holds, in GiB, and the VM's slots and speed."""

    pool: int
    used: int
    slots: int
    # The data disks attached today, which keep their slots.
    attached: int
    uncached_mbps: Optional[int]
    v2: bool


@dataclass(frozen=True)
class Gp3Layout:
    """An AWS pool: how many equal gp3 volumes, and the GiB, IOPS and MB/s of each."""

    count: int
    size: int
    iops: int
    mbps: int


def peak_of(hourlies: Sequence[Hourly], megabyte: float) -> Load:
    """Give the peak of the disks a pool replaces: their load added per hour, then each figure's busiest hour (MB/s)."""
    hours: Dict[str, Load] = {}
    for hourly in hourlies:
        for stamp, load in hourly.items():
            hours[stamp] = hours[stamp].plus(load) if stamp in hours else load
    return Load(
        max(hour.iops for hour in hours.values()),
        max(hour.throughput for hour in hours.values()) / megabyte,
        max(hour.write_iops for hour in hours.values()),
        max(hour.write_throughput for hour in hours.values()) / megabyte,
    )


def ceil_div(numerator: int, divisor: int) -> int:
    """Divide, rounding up."""
    return -(-numerator // divisor)


def pool_size_gib(used_gib: int) -> int:
    """Give the pool's size: today's data plus headroom of 20% of it, at least 50 GB."""
    return math.ceil(used_gib + max(HEADROOM_FRACTION * used_gib, HEADROOM_MIN_GIB))


def tier_up(gib: int, v2: bool) -> int:
    """Size a disk: any size on Premium SSD v2; on Premium SSD the smallest at or above gib, at most the largest."""
    return gib if v2 else next((tier for tier in V1_TIER_GIB if tier >= gib), V1_TIER_GIB[-1])


def tier_down(gib: int, v2: bool) -> int:
    """Size a disk: any size on Premium SSD v2; on Premium SSD the largest at or below gib, at least the smallest."""
    return gib if v2 else max((tier for tier in V1_TIER_GIB if tier <= gib), default=V1_TIER_GIB[0])


def elastic_floor(v2: bool) -> int:
    """Give the smallest elastic member."""
    return ELASTIC_FLOOR_V2_GIB if v2 else ELASTIC_FLOOR_V1_GIB


def evacuation_cap(uncached_mbps: Optional[int]) -> Optional[int]:
    """Give the largest disk the VM can empty in the evacuation window, in GiB; None when its speed is not known."""
    if uncached_mbps is None:
        return None
    window = EVAC_WINDOW_MINUTES * SECONDS_PER_MINUTE * EVAC_SHARE * uncached_mbps
    return math.floor(window / (2.0 * ELASTIC_FILL_FRACTION * MIB_PER_GIB))


def elastic_cap_gib(uncached_mbps: Optional[int], floor: int) -> int:
    """Give the largest elastic member the VM can empty in the evacuation window."""
    cap = evacuation_cap(uncached_mbps)
    return ELASTIC_CAP_GIB if cap is None else max(floor, min(ELASTIC_CAP_GIB, cap))


def clamp_elastics(count: int) -> int:
    """Keep a count of elastics within three to six."""
    return max(MIN_ELASTICS, min(MAX_ELASTICS, count))


def full_geometry(pool: int, used: int, uncached_mbps: Optional[int], v2: bool) -> List[int]:
    """Give two anchors and three to six equal elastics (data member sizes): the shape when the slots allow it."""
    floor = elastic_floor(v2)
    quantum = tier_up(max(floor, min(elastic_cap_gib(uncached_mbps, floor), pool // ELASTIC_DIVISOR)), v2)
    if pool < ANCHOR_POOL_THRESHOLD_GIB:
        quantum = max(quantum, tier_up(ceil_div(pool, MAX_ELASTICS), v2))
        return [quantum] * clamp_elastics(ceil_div(pool, quantum))
    by_band = half_up(pool * ANCHOR_CAP_FRACTION)
    joint = min(by_band, used) if used > 0 else by_band
    anchor = tier_down(max(1, min(joint // ANCHOR_COUNT, ANCHOR_MAX_GIB)), v2)
    remaining = max(0, pool - anchor * ANCHOR_COUNT)
    return [anchor] * ANCHOR_COUNT + [quantum] * clamp_elastics(ceil_div(remaining, quantum))


def covering_quantum(span: int, count: int, v2: bool) -> int:
    """Give the elastic size that covers span with count elastics, at least the smallest elastic."""
    floor = elastic_floor(v2)
    return floor if span <= 0 else max(floor, tier_up(ceil_div(span, count), v2))


def degraded_geometry(
    pool: int, uncached_mbps: Optional[int], member_slots: int, v2: bool
) -> Optional[Tuple[List[int], int]]:
    """Give one anchor and up to three elastics, and the small members: the shape when the VM has fewer free slots."""
    small = 1 if v2 else 0
    elastics = min(DEGRADED_MAX_ELASTICS, member_slots - small - DEGRADED_ANCHORS)
    if elastics < 1:
        small = 0
        elastics = min(DEGRADED_MAX_ELASTICS, member_slots - DEGRADED_ANCHORS)
    if elastics < 1:
        return None
    ceiling = tier_down(elastic_cap_gib(uncached_mbps, elastic_floor(v2)), v2)
    anchor = tier_down(min(half_up(pool * ANCHOR_CAP_FRACTION), ANCHOR_MAX_GIB), v2)
    quantum = covering_quantum(pool - anchor, elastics, v2)
    if quantum > ceiling:
        anchor = min(ANCHOR_MAX_GIB, tier_up(max(1, pool - elastics * ceiling), v2))
        quantum = covering_quantum(pool - anchor, elastics, v2)
    if anchor <= 0 or quantum <= 0 or quantum > ceiling or anchor + quantum * elastics < pool:
        return None
    return [anchor] + [quantum] * elastics, small


def pool_members(room: AzureRoom) -> Optional[Tuple[List[int], int]]:
    """Give the data members' sizes in GiB and the number of small members; None when the VM can't host the pool.

    Only a Premium SSD v2 pool has the small member.
    """
    full = full_geometry(room.pool, room.used, room.uncached_mbps, room.v2)
    small = 1 if room.v2 else 0
    member_slots = max(0, room.slots - room.attached - RESERVED_SLOTS)
    if len(full) + small <= member_slots:
        return full, small
    if room.slots < MIN_VM_SLOTS or member_slots < MIN_POOL_SLOTS:
        return None
    return degraded_geometry(room.pool, room.uncached_mbps, member_slots, room.v2)


def member_iops(demand: float, members: int, size: int, vm_iops: Optional[int]) -> int:
    """Give a Premium SSD v2 member's IOPS: its share of the peak x 1.3, within its size's and the VM's limits."""
    iops = max(V2_MIN_IOPS, math.ceil(DEMAND_FACTOR * demand / members))
    iops = min(iops, max(V2_MIN_IOPS, size * V2_MAX_IOPS_PER_GIB))
    if vm_iops is not None:
        iops = min(iops, max(V2_MIN_IOPS, vm_iops // members))
    return iops


def member_mbps(demand: float, members: int, iops: int, vm_mbps: Optional[int]) -> int:
    """Give a Premium SSD v2 member's MB/s: its share of the peak x 1.3, within its IOPS's and the VM's limits."""
    mbps = max(V2_MIN_MBPS, math.ceil(DEMAND_FACTOR * demand / members))
    mbps = min(mbps, max(V2_MIN_MBPS, min(V2_MAX_MBPS, int(iops * V2_MBPS_PER_IOPS))))
    if vm_mbps is not None:
        mbps = min(mbps, max(V2_MIN_MBPS, vm_mbps // members))
    return mbps


def hold_gib(used_bytes: int) -> int:
    """Give the size a new AWS pool is held at, in GiB.

    That is its data plus btrfs's 3%, at most 90% full (the band's high edge) and below the urgent level (95%), with at
    least 10 GiB free. One reading has no growth or burst to add.
    """
    held = math.ceil(used_bytes * (1 + BTRFS_OVERHEAD))
    return math.ceil(max(held / BAND_HIGH, held / URGENT_USED, held + FREE_FLOOR_GIB * GIB) / GIB)


def gp3_need(peak: Load, members: int) -> Tuple[int, int]:
    """Give the IOPS and MB/s one gp3 volume needs.

    That is the peak x 1.3 shared across the pool, at least the write peak x 1.3, at least its baseline, and IOPS
    enough for its MB/s.
    """
    iops = max(GP3_BASE_IOPS, margin(peak.write_iops), ceil_div(margin(peak.iops), members))
    mbps = max(GP3_BASE_MBPS, margin(peak.write_throughput), ceil_div(margin(peak.throughput), members))
    return max(iops, math.ceil(mbps / GP3_MBPS_PER_IOPS)), mbps


def margin(figure: float) -> int:
    """Give a measured figure with its 30% margin, rounded up."""
    return math.ceil(DEMAND_FACTOR * figure)


def gp3_members(limits: EbsLimits, used_bytes: int) -> Optional[Tuple[int, int]]:
    """Give how many equal gp3 volumes the pool takes and the GiB of each; None when the free slots can't hold them.

    One slot is kept spare. There are at least 2 volumes, each no larger than the instance can empty in the evacuation
    window, unless the slots need them larger.
    """
    member_slots = limits.free_slots - RESERVED_SLOTS
    if member_slots < 1:
        return None
    target = hold_gib(used_bytes)
    per_slot = ceil_div(target, member_slots)
    cap = evacuation_cap(limits.uncached_mbps)
    count = max(MIN_MEMBERS, ceil_div(target, per_slot if cap is None else max(cap, per_slot)))
    if count > member_slots:
        return None
    return count, ceil_div(target, count)


def gp3_layout(limits: EbsLimits, used_bytes: int, peak: Load) -> Optional[Gp3Layout]:
    """Give the gp3 pool for the instance; None when its free slots can't hold it.

    Raises NotEstimated when the pool would be too large, and Overloaded when its volumes can't carry the load.
    """
    members = gp3_members(limits, used_bytes)
    if members is None:
        return None
    count, size = members
    need_iops, need_mbps = gp3_need(peak, count)
    top_iops = max(GP3_BASE_IOPS, min(GP3_MAX_IOPS, size * GP3_IOPS_PER_GIB))
    top_mbps = max(GP3_BASE_MBPS, min(GP3_MAX_MBPS, math.floor(top_iops * GP3_MBPS_PER_IOPS)))
    if size > GP3_MAX_GIB:
        raise NotEstimated(TOO_MUCH_DATA)
    if need_iops > top_iops or need_mbps > top_mbps:
        raise Overloaded(TOO_BUSY)
    iops = min(need_iops, top_iops)
    if limits.uncached_iops is not None:
        iops = min(iops, max(GP3_BASE_IOPS, limits.uncached_iops // count))
    mbps = min(need_mbps, top_mbps)
    if limits.uncached_mbps is not None:
        mbps = min(mbps, max(GP3_BASE_MBPS, limits.uncached_mbps // count))
    return Gp3Layout(count, size, iops, min(mbps, max(GP3_BASE_MBPS, math.floor(iops * GP3_MBPS_PER_IOPS))))


def v2_cost(prices: DiskPrices, region: str, limits: AzureLimits, members: Sequence[int], peak: Load) -> float:
    """Price a Premium SSD v2 pool: each member's size, and the IOPS and MB/s above its baseline that the peak needs."""
    cost = 0.0
    for size in members:
        iops = member_iops(peak.iops, len(members), size, limits.uncached_iops)
        mbps = member_mbps(peak.throughput, len(members), iops, limits.uncached_mbps)
        cost += prices.monthly(region, DiskSpec(PREMIUM_V2, size, "", iops, mbps))
    return cost


def v1_cost(prices: DiskPrices, region: str, members: Sequence[int]) -> float:
    """Price a Premium SSD pool: each member at the list price of its size."""
    cost = 0.0
    for size in members:
        cost += prices.monthly(region, DiskSpec(PREMIUM_V1, size, "", None, None))
    return cost


def v1_short(limits: AzureLimits, members: Sequence[int], peak: Load) -> bool:
    """Tell whether a Premium SSD pool falls short of the peak x 1.3, or of what the VM can drive if that is less."""
    need_iops, need_mbps = DEMAND_FACTOR * peak.iops, DEMAND_FACTOR * peak.throughput
    if limits.uncached_iops is not None:
        need_iops = min(need_iops, limits.uncached_iops)
    if limits.uncached_mbps is not None:
        need_mbps = min(need_mbps, limits.uncached_mbps)
    has_iops = sum(V1_TIER_PERFORMANCE[tier_up(size, False)][0] for size in members)
    has_mbps = sum(V1_TIER_PERFORMANCE[tier_up(size, False)][1] for size in members)
    return need_iops > has_iops or need_mbps > has_mbps


def azure_pool(prices: DiskPrices, region: str, attached: int, limits: AzureLimits, demand: Demand) -> Optional[Pool]:
    """Give the Premium SSD v2 or Premium SSD pool for the VM; None when its free slots can't hold it.

    Raises NotEstimated with the reason when the pool is not estimated, Overloaded when it can't carry the load.
    """
    used = sum(max(1, math.ceil(used_bytes / GIB)) for used_bytes in demand.used)
    pool = pool_size_gib(used)
    layout = pool_members(AzureRoom(pool, used, limits.slots, attached, limits.uncached_mbps, limits.v2))
    if layout is None:
        return None
    data_members, small = layout
    if sum(data_members) < pool:
        raise NotEstimated(TOO_MUCH_DATA)
    members = data_members + [SMALL_MEMBER_GIB] * small
    if limits.v2:
        return Pool(sum(members), v2_cost(prices, region, limits, members, demand.peak))
    if v1_short(limits, members, demand.peak):
        raise Overloaded(TOO_BUSY)
    return Pool(sum(members), v1_cost(prices, region, members))


def aws_pool(prices: DiskPrices, region: str, limits: EbsLimits, demand: Demand) -> Optional[Pool]:
    """Give the gp3 pool for the instance; None when its free slots can't hold it.

    Raises NotEstimated with the reason when the pool is not estimated, Overloaded when it can't carry the load.
    """
    layout = gp3_layout(limits, sum(demand.used), demand.peak)
    if layout is None:
        return None
    each = prices.monthly(region, DiskSpec(GP3, layout.size, "", layout.iops, layout.mbps))
    return Pool(layout.count * layout.size, layout.count * each)
