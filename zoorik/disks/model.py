"""The disk part's values: VMs and their data disks as the clouds describe them, and what the part finds on each."""

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple, Union

from zoorik.format import cents, half_up
from zoorik.pricing.disks import DiskSpec


@dataclass(frozen=True)
class Load:
    """Disk activity: IOPS and throughput, in all and of writes alone; throughput's unit is said where it is used."""

    iops: float
    throughput: float
    write_iops: float
    write_throughput: float

    def plus(self, other: "Load") -> "Load":
        """Add another load to this one, figure by figure."""
        return Load(
            self.iops + other.iops,
            self.throughput + other.throughput,
            self.write_iops + other.write_iops,
            self.write_throughput + other.write_throughput,
        )


# The busiest minute of each hour of the metric window, by the hour's time stamp, with throughput in bytes per second.
Hourly = Mapping[str, Load]


@dataclass(frozen=True)
class Disk:
    """A managed disk or EBS volume, as its cloud describes it."""

    id: str
    cloud: str
    region: str
    sku: str
    size_gb: int
    # The performance tier set on an Azure Premium SSD, such as P30; empty when none is set, and on AWS.
    tier: str
    iops: Optional[int]
    mbps: Optional[int]
    # Azure Disk Encryption is on.
    ade: bool
    # It can be attached to more than one VM at once.
    shared: bool

    @property
    def spec(self) -> DiskSpec:
        """Give the disk as its list price sees it."""
        return DiskSpec(self.sku, self.size_gb, self.tier, self.iops, self.mbps)


@dataclass(frozen=True)
class DataDisk:
    """One data disk of a VM: where it is attached, its size and its record, or why it has no record."""

    # Its LUN on Azure; on AWS, its place among the instance's data volumes in device-name order.
    lun: int
    # None when its record couldn't be read, so its size is not known.
    size_gb: Optional[int]
    disk: Optional[Disk]
    # Why it has no record, such as an unmanaged (VHD) disk; empty when it has one.
    note: str
    # The device name it is attached at, on AWS; empty on Azure.
    device: str


@dataclass(frozen=True)
class AzureLimits:
    """What an Azure VM size allows its data disks, and whether Premium SSD v2 is offered where the VM runs."""

    slots: int
    # The size's uncached disk IOPS and MB/s; None where it lists none.
    uncached_iops: Optional[int]
    uncached_mbps: Optional[int]
    v2: bool


@dataclass(frozen=True)
class EbsLimits:
    """What an EC2 instance type allows its data volumes, as the disk method counts them."""

    slots: int
    # The slots left free beside today's volumes, device names counted.
    free_slots: int
    # The type's EBS baseline IOPS and MB/s; None for a type that lists none.
    uncached_iops: Optional[int]
    uncached_mbps: Optional[int]
    # The type attaches EBS volumes as NVMe devices: a Nitro instance type.
    nitro: bool


Limits = Union[AzureLimits, EbsLimits]


@dataclass(frozen=True)
class Vm:
    """A VM or EC2 instance with data disks, as its cloud describes it."""

    id: str
    name: str
    cloud: str
    # The subscription's id on Azure; the account on AWS.
    account: str
    region: str
    # Its availability zone on Azure; None for a VM in none, and on AWS.
    zone: Optional[str]
    size: str
    os_type: str
    # Such as Running or Stopped.
    power: str
    data: Tuple[DataDisk, ...]
    # The resource type that runs its resource group, such as Microsoft.Databricks/workspaces; None when none does.
    managed_by: Optional[str]
    # Why the run can't read inside it, such as an instance Systems Manager doesn't manage; None when it can.
    channel: Optional[str]
    # What its size allows; None when that couldn't be read, with the reason in facts_note.
    limits: Optional[Limits]
    facts_note: str

    @property
    def linux(self) -> bool:
        """Tell whether the VM runs Linux."""
        return self.os_type.lower() == "linux"

    @property
    def running(self) -> bool:
        """Tell whether the VM is running."""
        return self.power == "Running"


@dataclass(frozen=True)
class Guest:
    """What the read inside a VM found: its kernel, btrfs, the mount points on each data disk, and their used bytes."""

    kernel: str
    # loaded, module or absent.
    btrfs: str
    # The mount points on each data disk, by its LUN, in the order the command printed them.
    luns: Mapping[int, Tuple[str, ...]]
    mounts: Mapping[str, int]


@dataclass(frozen=True)
class Inside:
    """What the run learned inside a VM: what the read found, or why it has nothing."""

    guest: Optional[Guest]
    note: str


@dataclass(frozen=True)
class Price:
    """A disk's list price a month, or why it has none."""

    monthly: Optional[float]
    note: str


@dataclass(frozen=True)
class Activity:
    """A disk's busiest minute of each hour of the metric window, or why it has none."""

    hourly: Optional[Hourly]
    note: str


@dataclass(frozen=True)
class Saving:
    """A VM's saving: the disks a pool replaces and what they cost a month, the pool, and the priced disks that stay."""

    pooled: Tuple[Disk, ...]
    today: float
    pool_gb: int
    pool_cost: float
    # The VM's other priced data disks that no other VM shares: they cost the same today and after.
    staying: Tuple[Disk, ...]
    staying_cost: float

    @property
    def monthly(self) -> float:
        """Give the saving a month."""
        return cents(self.today - self.pool_cost)


@dataclass(frozen=True)
class VmResult:
    """What the disk part found on one VM: its used space, its cost today and its saving, or why it has none."""

    vm: Vm
    reasons: Tuple[str, ...]
    # GB used on the mounts the pool takes; None where they couldn't be measured.
    used_gb: Optional[float]
    # What all its data disks cost a month; None when one has no price, with the first such reason in price_note.
    current: Optional[float]
    price_note: str
    saving: Optional[Saving]


@dataclass(frozen=True)
class NotAssessed:
    """A VM the disk part leaves out with the reason, such as one it couldn't tell apart from a cluster node."""

    cloud: str
    account: str
    region: str
    name: str
    reason: str


@dataclass(frozen=True)
class Unread:
    """A subscription or region the disk part couldn't read, and why."""

    cloud: str
    name: str
    reason: str


@dataclass(frozen=True)
class ScopeRead:
    """What one subscription or region held: all its VMs and all its disks, counted."""

    name: str
    vms: int
    disks: int


@dataclass(frozen=True)
class Inventory:
    """One cloud's VMs with data disks, cluster nodes left out, and what each subscription or region held."""

    vms: Tuple[Vm, ...]
    not_assessed: Tuple[NotAssessed, ...]
    read: Tuple[ScopeRead, ...]
    unread: Tuple[Unread, ...]
    # The subscriptions' names by id, on Azure.
    names: Mapping[str, str]


@dataclass(frozen=True)
class Tally:
    """Money in whole dollars and space in GB of the data disks of VMs in the saving.

    Each VM's cost today and after is rounded first, so every sum adds up.
    """

    today: int
    after: int
    paid: int
    pool: int
    disks: int

    @property
    def saving(self) -> int:
        """Give the saving a month."""
        return self.today - self.after


def tally(results: Sequence[VmResult]) -> Tally:
    """Add up the VMs in the saving: their disks today, the pools and the disks that stay, after."""
    today = after = paid = pool = disks = 0
    for saving in (result.saving for result in results if result.saving is not None):
        same = half_up(saving.staying_cost)
        staying_gb = sum(disk.size_gb for disk in saving.staying)
        today += half_up(saving.today) + same
        after += half_up(saving.pool_cost) + same
        paid += sum(disk.size_gb for disk in saving.pooled) + staying_gb
        pool += saving.pool_gb + staying_gb
        disks += len(saving.pooled) + len(saving.staying)
    return Tally(today, after, paid, pool, disks)


@dataclass(frozen=True)
class DisksResult:
    """The disk part's result: each VM with data disks, the VMs and scopes left out, and the cost today."""

    vms: Tuple[VmResult, ...]
    not_assessed: Tuple[NotAssessed, ...]
    unread: Tuple[Unread, ...]
    # Azure subscriptions' names by id.
    subscriptions: Mapping[str, str]
    # Page 1's disk cost today in whole dollars: each priced data disk of the VMs listed, once, rounded half up.
    today: int

    @property
    def in_saving(self) -> Tuple[VmResult, ...]:
        """Give the VMs with a saving."""
        return tuple(result for result in self.vms if result.saving is not None)

    @property
    def saving(self) -> int:
        """Give the disk saving a month, in whole dollars."""
        return tally(self.in_saving).saving

    @property
    def after(self) -> int:
        """Give the disk cost after the saving, in whole dollars."""
        return self.today - self.saving
