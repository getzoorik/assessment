"""Azure managed-disk list prices, from the Azure Retail Prices API, read once per region.

Premium SSD, Standard SSD and Standard HDD are priced by size tier. Premium SSD v2 and Ultra Disk are charged each
hour for the GiB, IOPS and MB/s they are provisioned with, on graduated meters.
"""

import math
import re
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple

from zoorik.errors import PriceNotFound
from zoorik.format import cents
from zoorik.pricing.disks import DiskSpec
from zoorik.pricing.machines import HOURS_PER_MONTH
from zoorik.pricing.retail import RetailItem, retail_items
from zoorik.system.http import HttpClient
from zoorik.system.memo import Memo

# Each size tier: the largest size it holds in GiB, and its number (P1, P2 ... P80). Standard HDD starts at S4.
TIER_SIZES = (
    (4, "1"),
    (8, "2"),
    (16, "3"),
    (32, "4"),
    (64, "6"),
    (128, "10"),
    (256, "15"),
    (512, "20"),
    (1024, "30"),
    (2048, "40"),
    (4096, "50"),
    (8192, "60"),
    (16384, "70"),
    (32767, "80"),
)
HDD_TIER_SIZES = TIER_SIZES[3:]
TIER_PRODUCTS = ("Premium SSD Managed Disks", "Standard SSD Managed Disks", "Standard HDD Managed Disks")
TIER_METER = re.compile(r"([PES]\d+) (LRS|ZRS) Disk")
PREMIUM_TIER = re.compile(r"P\d+")
# The provisioned products, and the start their meter names share.
PROVISIONED_PRODUCTS = {"Azure Premium SSD v2": "Premium LRS ", "Ultra Disks": "Ultra LRS "}
PROVISIONED_METERS = {
    "Provisioned Capacity": "capacity",
    "Provisioned IOPS": "iops",
    "Provisioned Throughput (MBps)": "mbps",
}
PROVISIONED_UNITS = ("1 GiB/Hour", "1/Hour")
PROVISIONED_FAMILIES = ("V2", "ULTRA")

# The hourly price of each unit, by the usage it starts at.
Graduated = Mapping[float, float]


@dataclass(frozen=True)
class DiskType:
    """How a managed-disk SKU is priced: its family (P, E, S, V2 or ULTRA), redundancy and name for the reader."""

    family: str
    redundancy: str
    label: str


DISK_TYPES = {
    "premium_lrs": DiskType("P", "LRS", "Premium SSD"),
    "premium_zrs": DiskType("P", "ZRS", "Premium SSD ZRS"),
    "standardssd_lrs": DiskType("E", "LRS", "Standard SSD"),
    "standardssd_zrs": DiskType("E", "ZRS", "Standard SSD ZRS"),
    "standard_lrs": DiskType("S", "LRS", "Standard HDD"),
    "premiumv2_lrs": DiskType("V2", "LRS", "Premium SSD v2"),
    "ultrassd_lrs": DiskType("ULTRA", "LRS", "Ultra Disk"),
}


@dataclass(frozen=True)
class RegionDiskPrices:
    """One region's managed-disk prices."""

    # US dollars a month, by tier and redundancy, such as P30 LRS.
    tiers: Mapping[str, float]
    # The graduated meters of Premium SSD v2 and of Ultra Disk, by what they charge for: capacity, iops or mbps.
    v2: Mapping[str, Graduated]
    ultra: Mapping[str, Graduated]


def graduated(units: float, tiers: Graduated) -> float:
    """Price units on a graduated meter: each tier's price applies from its first unit to the next tier's."""
    steps = sorted(tiers.items())
    total = 0.0
    for index, (start, price) in enumerate(steps):
        end = steps[index + 1][0] if index + 1 < len(steps) else math.inf
        if units > start:
            total += (min(units, end) - start) * price
    return total


def size_tier(family: str, size_gb: int) -> Optional[str]:
    """Name the tier Azure prices a disk at, the smallest at or above its size; None above the largest."""
    for largest, number in HDD_TIER_SIZES if family == "S" else TIER_SIZES:
        if size_gb <= largest:
            return family + number
    return None


def performance_tier(tier: str, disk: DiskSpec) -> str:
    """Give the tier a Premium SSD is priced at: its size's tier, or the higher performance tier set on it."""
    numbers = [number for _, number in TIER_SIZES]
    chosen = disk.tier[1:]
    if PREMIUM_TIER.fullmatch(disk.tier) and chosen in numbers and numbers.index(chosen) > numbers.index(tier[1:]):
        return disk.tier
    return tier


class AzureDiskPrices:
    """Azure's managed-disk prices."""

    def __init__(self, http: HttpClient) -> None:
        """Read each region's prices through http, once."""
        self._http = http
        self._regions: Memo[str, RegionDiskPrices] = Memo(self._read)

    def monthly(self, region: str, disk: DiskSpec) -> float:
        """Give the disk's list price per month: by its tier, or by what it is provisioned with."""
        kind = DISK_TYPES.get(disk.sku.lower())
        if kind is None:
            raise PriceNotFound(f"{disk.sku or 'unknown'} disks are not priced")
        prices = self._regions.get(region)
        if kind.family in PROVISIONED_FAMILIES:
            meters = prices.v2 if kind.family == "V2" else prices.ultra
            return provisioned_monthly(meters, kind, region, disk)
        return tier_monthly(prices.tiers, kind, region, disk)

    def _read(self, region: str) -> RegionDiskPrices:
        """Read every managed-disk meter of a region."""
        products = " or ".join(f"productName eq '{product}'" for product in (*TIER_PRODUCTS, *PROVISIONED_PRODUCTS))
        query = f"armRegionName eq '{region}' and priceType eq 'Consumption' and ({products})"
        tiers: Dict[str, float] = {}
        provisioned: Dict[str, Dict[str, Dict[float, float]]] = {product: {} for product in PROVISIONED_PRODUCTS}
        for item in retail_items(self._http, query):
            tier = tier_price(item)
            if tier is not None:
                tiers[tier[0]] = tier[1]
            meter = provisioned_meter(item)
            if meter is not None:
                provisioned[item.product].setdefault(meter, {})[item.tier_minimum] = item.price
        return RegionDiskPrices(tiers, provisioned["Azure Premium SSD v2"], provisioned["Ultra Disks"])


def tier_price(item: RetailItem) -> Optional[Tuple[str, float]]:
    """Read a size tier's monthly price, such as P30 LRS, from a retail item; None for any other item."""
    match = TIER_METER.fullmatch(item.meter)
    if match is None or item.product not in TIER_PRODUCTS or item.unit != "1/Month":
        return None
    return f"{match.group(1)} {match.group(2)}", item.price


def provisioned_meter(item: RetailItem) -> Optional[str]:
    """Name what a provisioned disk's retail item charges for (capacity, iops or mbps); None for any other item."""
    prefix = PROVISIONED_PRODUCTS.get(item.product)
    if prefix is None or not item.meter.startswith(prefix) or item.unit not in PROVISIONED_UNITS:
        return None
    return PROVISIONED_METERS.get(item.meter[len(prefix) :])


def tier_monthly(tiers: Mapping[str, float], kind: DiskType, region: str, disk: DiskSpec) -> float:
    """Price a disk by its size tier."""
    tier = size_tier(kind.family, disk.size_gb)
    if tier is None:
        raise PriceNotFound(f"{disk.size_gb:,} GB is above the largest {kind.label} tier")
    if kind.family == "P":
        tier = performance_tier(tier, disk)
    monthly = tiers.get(f"{tier} {kind.redundancy}")
    if monthly is None:
        raise PriceNotFound(f"no list price for {tier} {kind.redundancy} in {region}")
    return cents(monthly)


def provisioned_monthly(meters: Mapping[str, Graduated], kind: DiskType, region: str, disk: DiskSpec) -> float:
    """Price a disk charged each hour for the GiB, IOPS and MB/s it is provisioned with."""
    if disk.iops is None or disk.mbps is None:
        raise PriceNotFound("provisioned IOPS or MB/s not reported")
    if not all(meters.get(unit) for unit in PROVISIONED_METERS.values()):
        raise PriceNotFound(f"no {kind.label} list price in {region}")
    hourly = (
        graduated(disk.size_gb, meters["capacity"])
        + graduated(disk.iops, meters["iops"])
        + graduated(disk.mbps, meters["mbps"])
    )
    return cents(hourly * HOURS_PER_MONTH)
