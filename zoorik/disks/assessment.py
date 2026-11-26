"""The disk part of the run: each cloud's VMs with data disks, the read inside them after a yes, and each one's saving.

A VM's saving is what a pool the right size would save on its data disks, at list prices. A VM without one keeps the
reasons.
"""

import re
from dataclasses import dataclass, replace
from typing import Dict, List, Mapping, Optional, Protocol, Sequence, Set, Tuple, Union

from zoorik.cloud.aws import AwsCli, AwsScope
from zoorik.cloud.azure import AzureCli, AzureScope
from zoorik.cloud.keys import AWS, AZURE, CLOUD_NAMES
from zoorik.console import ConsolePart, Progress
from zoorik.disks.aws_inventory import AwsInventory
from zoorik.disks.azure_inventory import AzureInventory
from zoorik.disks.guest import GuestReader, RunCommandReader, SsmReader, kernel_version
from zoorik.disks.metrics import METRIC_DAYS, AzureMetrics, DiskMetrics, EbsMetrics, metric_window
from zoorik.disks.model import (
    Activity,
    AzureLimits,
    DataDisk,
    Disk,
    DisksResult,
    EbsLimits,
    Guest,
    Hourly,
    Inside,
    Inventory,
    Limits,
    Load,
    NotAssessed,
    Price,
    Saving,
    Unread,
    Vm,
    VmResult,
)
from zoorik.disks.pools import (
    MEGABYTE,
    MIN_VM_SLOTS,
    Demand,
    NotEstimated,
    Overloaded,
    Pool,
    aws_pool,
    azure_pool,
    peak_of,
)
from zoorik.errors import AssessmentError, PriceNotFound
from zoorik.format import GIB, cents, count, half_up, usd
from zoorik.pricing.disks import DiskPrices
from zoorik.system.clock import Clock
from zoorik.system.prompt import Prompt
from zoorik.system.workers import parallel

KERNEL_FLOOR = (5, 14)
BTRFS_READY = ("loaded", "module")
NO_PRICE = "no list price"
NO_SLOTS = "too few free data-disk slots to add the pool beside today's disks"
SHARED = "its data disks are shared with other VMs: they stay as they are"
NOT_MOUNTED = "no data disk is mounted inside the VM"
ENCRYPTION = "needs work first: move from Azure Disk Encryption to server-side encryption"
NO_DISK = {AZURE: "no managed data disk", AWS: "data volumes not readable"}
UNMATCHED = {
    AZURE: "its data disks could not be matched inside the VM",
    AWS: "its volumes could not be matched inside the instance",
}
# How each cloud's lines name a scope, a VM and a disk.
SCOPE_WORDS = {AZURE: ("Subscription", "VM", "disk"), AWS: ("Region", "instance", "volume")}
# What each cloud's inventory reads a few at a time, for the status line.
INVENTORY_PROGRESS = {AZURE: "VM size lists", AWS: "regions"}
MICROSOFT = re.compile(r"^Microsoft[.]")

# The mounts a pool could take, each with the LUNs of the data disks under it.
Mounts = Dict[str, List[int]]


class VmInventory(Protocol):
    """A cloud's VMs with data disks."""

    def read(self, progress: Progress) -> Inventory:
        """Read the VMs, reporting progress."""


@dataclass(frozen=True)
class DiskCloud:
    """One cloud as the disk part reads it: its VMs, the read inside them, their disks' metrics and list prices."""

    key: str
    inventory: VmInventory
    guest: GuestReader
    metrics: DiskMetrics
    prices: DiskPrices


def azure_disks(cli: AzureCli, scope: AzureScope, prices: DiskPrices) -> DiskCloud:
    """Read the VMs of scope's subscriptions through cli, and price their disks with prices."""
    names = {sub.id: sub.name for sub in scope.subscriptions}
    return DiskCloud(AZURE, AzureInventory(cli, scope), RunCommandReader(cli, names), AzureMetrics(cli), prices)


def aws_disks(cli: AwsCli, scope: AwsScope, prices: DiskPrices, clock: Clock) -> DiskCloud:
    """Read the instances of scope's regions through cli, waiting on clock, and price their volumes with prices."""
    return DiskCloud(AWS, AwsInventory(cli, scope), SsmReader(cli, clock), EbsMetrics(cli), prices)


@dataclass(frozen=True)
class Figures:
    """What each disk costs and how busy it is, by disk id, and the price list a VM's pool is priced on."""

    prices: Mapping[str, Price]
    activity: Mapping[str, Activity]
    price_list: DiskPrices


@dataclass(frozen=True)
class Findings:
    """What the run found on a VM before sizing its pool.

    That is why a pool can't go on it (red), what must be done first (amber), what the reader should know (notes), and
    the mounts a pool could take, with their used space in GB when every one of them reports it.
    """

    red: Tuple[str, ...]
    amber: Tuple[str, ...]
    notes: Tuple[str, ...]
    mounts: Mounts
    used_gb: Optional[float]

    def reasons(self, *why: str) -> Tuple[str, ...]:
        """Give the VM's reasons, with why its estimate stopped, if it did, before the notes."""
        return (*self.red, *self.amber, *why, *self.notes)


@dataclass(frozen=True)
class Measured:
    """A data disk a pool would replace: its record, its list price a month and its busiest minute of each hour."""

    disk: Disk
    price: float
    hourly: Hourly


@dataclass(frozen=True)
class Case:
    """A VM ready for its pool: what the read inside it found, its limits, and its disks' figures."""

    vm: Vm
    guest: Guest
    limits: Limits
    # The disks under the mounts a pool could take, by LUN.
    measured: Mapping[int, Measured]
    figures: Figures


@dataclass(frozen=True)
class Sized:
    """What sizing a VM's pool gave: the GB used on the mounts it takes, and the saving, or why there is none."""

    used_gb: float
    saving: Optional[Saving]
    why: Tuple[str, ...]


class DisksAssessment:
    """The disk part: reads each cloud's VMs, asks before reading inside them, and works out each one's saving."""

    def __init__(self, clouds: Sequence[DiskCloud], clock: Clock, prompt: Prompt, console: ConsolePart) -> None:
        """Assess the VMs of clouds, in their order, asking on prompt and writing to console."""
        self._clouds = {cloud.key: cloud for cloud in clouds}
        self._clock = clock
        self._prompt = prompt
        self._console = console

    def run(self) -> DisksResult:
        """Assess every VM with data disks."""
        inventories = [self._inventory(cloud) for cloud in self._clouds.values()]
        vms = [vm for inventory in inventories for vm in inventory.vms]
        prices = self._prices(vms)
        inside = self._inside(vms)
        activity = self._activity(vms, inside)
        results = tuple(
            assess(vm, inside.get(vm.id), Figures(prices, activity, self._clouds[vm.cloud].prices)) for vm in vms
        )
        return DisksResult(
            vms=results,
            not_assessed=tuple(unsure for inventory in inventories for unsure in inventory.not_assessed),
            unread=tuple(scope for inventory in inventories for scope in inventory.unread),
            subscriptions={sub: name for inventory in inventories for sub, name in inventory.names.items()},
            today=cost_today(vms, prices),
        )

    def _inventory(self, cloud: DiskCloud) -> Inventory:
        """Read one cloud's VMs, and say what each subscription or region held, or why it couldn't be read."""
        inventory = cloud.inventory.read(self._console.progress(INVENTORY_PROGRESS[cloud.key]))
        for line in inventory_lines(cloud.key, inventory):
            self._console.line(line)
        return inventory

    def _prices(self, vms: Sequence[Vm]) -> Dict[str, Price]:
        """Price each data disk once, a few at a time; a price list that can't be read is named once for each cloud."""
        disks = list({data.disk.id: data.disk for vm in vms for data in vm.data if data.disk is not None}.values())
        results = parallel(self._monthly, disks, self._console.progress("disk prices"))
        prices: Dict[str, Price] = {}
        unreadable: Dict[str, List[str]] = {}
        for disk, result in zip(disks, results):
            if isinstance(result, PriceNotFound):
                prices[disk.id] = Price(None, str(result))
            elif isinstance(result, AssessmentError):
                prices[disk.id] = Price(None, f"price list not readable for {disk.region}")
                unreadable.setdefault(disk.cloud, []).append(str(result))
            else:
                prices[disk.id] = Price(result, "")
        for key in self._clouds:
            if key in unreadable:
                self._console.line(f"Disk list prices: {min(unreadable[key])}")
        return prices

    def _monthly(self, disk: Disk) -> float:
        """Give a disk's list price a month, from its cloud's price list."""
        return self._clouds[disk.cloud].prices.monthly(disk.region, disk.spec)

    def _inside(self, vms: Sequence[Vm]) -> Dict[str, Inside]:
        """Read inside each VM a pool could go on, after a yes for its cloud; say why the others aren't read.

        A VM its cloud already rules out is not read, and has no entry.
        """
        inside: Dict[str, Inside] = {}
        candidates: List[Vm] = []
        for vm in vms:
            if screen(vm):
                continue
            note = unreachable(vm)
            if note is None:
                candidates.append(vm)
            else:
                inside[vm.id] = Inside(None, note)
        for cloud in self._clouds.values():
            group = [vm for vm in candidates if vm.cloud == cloud.key]
            # A cloud with no VM to read inside asks nothing.
            if group:
                inside.update(self._ask_and_read(cloud, group))
        return inside

    def _ask_and_read(self, cloud: DiskCloud, vms: Sequence[Vm]) -> Dict[str, Inside]:
        """Ask y/N for one cloud's VMs, and on a yes read inside them, a few at a time."""
        with self._console.question():
            answer = self._prompt.ask(cloud.guest.question(vms))
        if answer is None:
            self._console.line(f"No terminal to ask on, so disk usage inside {CLOUD_NAMES[cloud.key]} VMs is not read.")
            return {vm.id: Inside(None, "no terminal to ask on") for vm in vms}
        if not answer:
            return {vm.id: Inside(None, "declined at the prompt") for vm in vms}
        results = parallel(cloud.guest.read, vms, self._console.progress(f"{SCOPE_WORDS[cloud.key][1]}s"))
        return {vm.id: inside_of(result) for vm, result in zip(vms, results)}

    def _activity(self, vms: Sequence[Vm], inside: Mapping[str, Inside]) -> Dict[str, Activity]:
        """Read 14 days of metrics for each disk a pool could take, once each, a few at a time."""
        disks = list({disk.id: disk for vm in vms for disk in measured_disks(vm, inside.get(vm.id))}.values())
        window = metric_window(self._clock)

        def hourly(disk: Disk) -> Hourly:
            return self._clouds[disk.cloud].metrics.hourly(disk, window)

        results = parallel(hourly, disks, self._console.progress("disk metrics"))
        return {disk.id: activity_of(result) for disk, result in zip(disks, results)}


def inventory_lines(key: str, inventory: Inventory) -> List[str]:
    """Say what each subscription or region held or why it couldn't be read, and which VMs are left out, and why."""
    label, vm_word, disk_word = SCOPE_WORDS[key]
    lines = [
        f"{label} {', '.join(names)}: {vm_word}s not listed, {reason}" for reason, names in by_reason(inventory.unread)
    ]
    lines += [
        f"{label} {scope.name}: {count(scope.vms, vm_word)}, {count(scope.disks, disk_word)}"
        for scope in inventory.read
        if scope.vms or scope.disks
    ]
    empty = sum(1 for scope in inventory.read if not scope.vms and not scope.disks)
    if empty:
        lines.append(f"{count(empty, label.lower())} with no {vm_word}s or {disk_word}s.")
    lines += [
        f"{count(len(names), vm_word)} not assessed, {reason}" for reason, names in by_reason(inventory.not_assessed)
    ]
    return lines


def by_reason(items: Sequence[Union[Unread, NotAssessed]]) -> List[Tuple[str, List[str]]]:
    """Give the names of the scopes or VMs left out for each reason, the reasons in the order they come."""
    names: Dict[str, List[str]] = {}
    for item in items:
        names.setdefault(item.reason, []).append(item.name)
    return list(names.items())


def inside_of(result: Union[Guest, AssessmentError]) -> Inside:
    """Give what the read inside a VM found, or why it failed."""
    if isinstance(result, AssessmentError):
        return Inside(None, str(result))
    return Inside(result, "")


def activity_of(result: Union[Hourly, AssessmentError]) -> Activity:
    """Give a disk's metrics, or why it has none."""
    if isinstance(result, AssessmentError):
        return Activity(None, f"metrics not readable: {result}")
    if not result:
        return Activity(None, f"no metric data in the last {METRIC_DAYS} days")
    return Activity(result, "")


def cost_today(vms: Sequence[Vm], prices: Mapping[str, Price]) -> int:
    """Give the data disks' cost a month in whole dollars: each priced disk once, however many VMs share it."""
    monthly = {data.disk.id: prices[data.disk.id].monthly for vm in vms for data in vm.data if data.disk is not None}
    return sum(half_up(price) for price in monthly.values() if price is not None)


def screen(vm: Vm) -> Tuple[str, ...]:
    """Give why a pool can't go on the VM, from what its cloud reports, before looking inside it."""
    red: List[str] = []
    if not vm.linux:
        red.append(f"{vm.os_type or 'Non-Linux'} VM: not estimated by this assessment yet")
    if vm.managed_by is not None:
        service = MICROSOFT.sub("", vm.managed_by.split("/")[0])
        red.append(f"run by another Azure service ({service}): it is left to that service")
    if isinstance(vm.limits, AzureLimits) and vm.limits.slots < MIN_VM_SLOTS:
        red.append(f"too few data-disk slots ({vm.limits.slots}): a pool needs {MIN_VM_SLOTS} or more")
    if isinstance(vm.limits, EbsLimits) and not vm.limits.nitro:
        red.append(f"{vm.size} is an older instance type: a pool needs a Nitro instance type")
    return tuple(red)


def unreachable(vm: Vm) -> Optional[str]:
    """Give why the run can't read inside the VM; None when it can."""
    if not vm.running:
        return "VM is not running"
    if all(data.disk is None for data in vm.data):
        return NO_DISK[vm.cloud]
    return vm.channel


def data_mounts(vm: Vm, guest: Guest) -> Mounts:
    """Give the mounts that sit on the VM's data disks, with the LUNs under each."""
    known = {data.lun for data in vm.data}
    mounts: Mounts = {}
    for lun, points in guest.luns.items():
        if lun in known:
            for point in points:
                mounts.setdefault(point, []).append(lun)
    return mounts


def poolable(vm: Vm, guest: Guest) -> Mounts:
    """Give the data mounts a pool can take: those on no shared disk."""
    shared = {data.lun for data in vm.data if data.disk is not None and data.disk.shared}
    return {mount: luns for mount, luns in data_mounts(vm, guest).items() if not shared & set(luns)}


def under(vm: Vm, mounts: Mounts) -> List[DataDisk]:
    """Give the VM's data disks under the mounts, each once, in the VM's order."""
    luns = {lun for points in mounts.values() for lun in points}
    return [data for data in vm.data if data.lun in luns]


def measured_disks(vm: Vm, inside: Optional[Inside]) -> List[Disk]:
    """Give the disks whose activity a pool on the VM needs: those under the mounts it could take."""
    if inside is None or inside.guest is None:
        return []
    return [data.disk for data in under(vm, poolable(vm, inside.guest)) if data.disk is not None]


def used_space(guest: Guest, mounts: Mounts) -> float:
    """Give the GB used on the mounts."""
    return sum(guest.mounts[mount] for mount in mounts) / GIB


def assess(vm: Vm, inside: Optional[Inside], figures: Figures) -> VmResult:
    """Work out one VM's used space and saving, or the reasons it has none."""
    guest, unread = matched(vm, inside)
    findings = examine(vm, guest)
    current, price_note = cost_now(vm, figures.prices)
    result = VmResult(vm, findings.reasons(), findings.used_gb, current, price_note, None)
    if findings.red:
        return result
    if guest is None:
        return replace(result, reasons=findings.reasons(f"disk usage not read: {unread}"))
    # A mount whose used space isn't reported can't be sized; the notes name it.
    if findings.used_gb is None:
        return result
    case = ready(vm, guest, findings.mounts, figures)
    if isinstance(case, str):
        return replace(result, reasons=findings.reasons(case))
    sized = size_pool(case, findings.mounts, findings.used_gb)
    return replace(result, reasons=findings.reasons(*sized.why), used_gb=sized.used_gb, saving=sized.saving)


def matched(vm: Vm, inside: Optional[Inside]) -> Tuple[Optional[Guest], str]:
    """Give what the read inside the VM found, or why there is nothing; a read that matched no data disk found none.

    A VM with no read inside it is one its cloud already rules out, whose reasons are known.
    """
    if inside is None:
        return None, ""
    if inside.guest is not None and not inside.guest.luns:
        return None, UNMATCHED[vm.cloud]
    return inside.guest, inside.note


def examine(vm: Vm, guest: Optional[Guest]) -> Findings:
    """Find what stops a pool on the VM, what must be done first, and the mounts it could take with their used space."""
    red = list(screen(vm))
    amber: List[str] = []
    mounts: Mounts = {}
    if guest is not None:
        mounts = poolable(vm, guest)
        red += inside_red(vm, guest, mounts)
        amber += kernel_work(guest)
    if any(data.disk is not None and data.disk.ade for data in vm.data):
        amber.append(ENCRYPTION)
    missing = [] if guest is None else [mount for mount in mounts if mount not in guest.mounts]
    notes = (f"used space on {missing[0]} not reported",) if missing else ()
    used_gb = None if guest is None or missing or not mounts else used_space(guest, mounts)
    return Findings(tuple(red), tuple(amber), notes, mounts, used_gb)


def inside_red(vm: Vm, guest: Guest, mounts: Mounts) -> List[str]:
    """Give why a pool can't go on the VM, from what the read inside it found."""
    red: List[str] = []
    if guest.btrfs not in BTRFS_READY:
        red.append(f"no btrfs in its Linux kernel ({guest.kernel}): a pool needs btrfs")
    # No mount a pool could take: its data disks are shared, or none is mounted.
    if not mounts:
        red.append(SHARED if data_mounts(vm, guest) else NOT_MOUNTED)
    return red


def kernel_work(guest: Guest) -> List[str]:
    """Give the kernel upgrade a pool needs first, if any."""
    version = kernel_version(guest.kernel)
    # A kernel whose version can't be read is not asked to upgrade.
    if version is not None and version < KERNEL_FLOOR:
        return [f"needs work first: upgrade its Linux kernel ({guest.kernel}) to 5.14 or later"]
    return []


def cost_now(vm: Vm, prices: Mapping[str, Price]) -> Tuple[Optional[float], str]:
    """Give what all the VM's data disks cost a month; None, with the first disk's reason, when one has no price."""
    total = 0.0
    for data in vm.data:
        if data.disk is None:
            return None, data.note
        price = prices[data.disk.id]
        if price.monthly is None:
            return None, price.note
        total += price.monthly
    return cents(total), ""


def ready(vm: Vm, guest: Guest, mounts: Mounts, figures: Figures) -> Union[Case, str]:
    """Give the VM ready for its pool, or why its pool can't be sized: its limits, or a disk's price or metrics."""
    if vm.limits is None:
        return f"VM size limits not readable: {vm.facts_note}"
    priced: List[Tuple[int, Disk, float]] = []
    for data in under(vm, mounts):
        price = None if data.disk is None else figures.prices[data.disk.id].monthly
        # A disk with no list price leaves nothing to set the pool's price against.
        if data.disk is None or price is None:
            return NO_PRICE
        priced.append((data.lun, data.disk, price))
    measured: Dict[int, Measured] = {}
    for lun, disk, price in priced:
        activity = figures.activity[disk.id]
        if activity.hourly is None:
            return f"disk activity not read: {activity.note}"
        measured[lun] = Measured(disk, price, activity.hourly)
    return Case(vm, guest, vm.limits, measured, figures)


def size_pool(case: Case, mounts: Mounts, used_gb: float) -> Sized:
    """Size the pool on the mounts, and price it against the disks it replaces."""
    try:
        pool, kept = plan_pool(case, mounts)
    except (NotEstimated, AssessmentError) as error:
        return Sized(used_gb, None, (str(error),))
    if pool is None:
        return Sized(used_gb, None, (NO_SLOTS,))
    return compare(case, kept, pool)


def plan_pool(case: Case, mounts: Mounts) -> Tuple[Optional[Pool], Mounts]:
    """Give the pool and the mounts it takes; None for the pool when the VM's free slots can't hold it.

    While the pool can't carry the load, the busiest mount (by peak IOPS, then MB/s) stays on its disks; when none is
    left to stay, the first reason stands.
    """
    kept = dict(mounts)
    first: Optional[Overloaded] = None
    while True:
        try:
            return pool_for(case, kept), kept
        except Overloaded as error:
            if first is None:
                first = error
            # With one mount left, none can stay behind on its disks.
            if len(kept) == 1:
                raise first from None
            del kept[busiest(case, kept)]


def pool_for(case: Case, mounts: Mounts) -> Optional[Pool]:
    """Size and price the pool for the mounts' data and their disks' peak; None when the free slots can't hold it."""
    demand = Demand(tuple(case.guest.mounts[mount] for mount in mounts), peak(case, mounts))
    if isinstance(case.limits, AzureLimits):
        return azure_pool(case.figures.price_list, case.vm.region, len(case.vm.data), case.limits, demand)
    return aws_pool(case.figures.price_list, case.vm.region, case.limits, demand)


def peak(case: Case, mounts: Mounts) -> Load:
    """Give the peak of the disks under the mounts, their load added per hour, with throughput in MB/s."""
    return peak_of([case.measured[data.lun].hourly for data in under(case.vm, mounts)], MEGABYTE[case.vm.cloud])


def busiest(case: Case, mounts: Mounts) -> str:
    """Name the mount whose disks are busiest: by peak IOPS, then MB/s."""

    def load(mount: str) -> Tuple[float, float]:
        alone = peak(case, {mount: mounts[mount]})
        return alone.iops, alone.throughput

    return max(mounts, key=load)


def compare(case: Case, kept: Mounts, pool: Pool) -> Sized:
    """Set the pool's price against the disks it replaces: the VM's saving, or why it has none."""
    pooled = under(case.vm, kept)
    today = cents(sum(case.measured[data.lun].price for data in pooled))
    pool_cost = cents(pool.monthly)
    used_gb = used_space(case.guest, kept)
    if today - pool_cost <= 0:
        why = (
            f"no saving: the pool would cost {usd(half_up(pool_cost))} a month, no less than these disks "
            f"({usd(half_up(today))})"
        )
        return Sized(used_gb, None, (why,))
    stay = staying(case.vm, {data.lun for data in pooled}, case.figures.prices)
    saving = Saving(
        pooled=tuple(case.measured[data.lun].disk for data in pooled),
        today=today,
        pool_gb=pool.size_gb,
        pool_cost=pool_cost,
        staying=tuple(disk for disk, _ in stay),
        staying_cost=sum((price for _, price in stay), 0.0),
    )
    return Sized(used_gb, saving, ())


def staying(vm: Vm, pooled: Set[int], prices: Mapping[str, Price]) -> List[Tuple[Disk, float]]:
    """Give the VM's other priced data disks that no other VM shares, with their prices: they stay as they are."""
    disks: List[Tuple[Disk, float]] = []
    for data in vm.data:
        price = None if data.disk is None else prices[data.disk.id].monthly
        # A shared disk belongs to more than one VM, and an unpriced one has no cost to carry over.
        if data.lun in pooled or data.disk is None or data.disk.shared or price is None:
            continue
        disks.append((data.disk, price))
    return disks
