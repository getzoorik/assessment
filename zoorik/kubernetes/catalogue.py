"""The machine types a region offers a subscription or account, as step 3 compares them.

Azure's come from the Compute SKUs of the subscription and region, AWS's from the region's instance types and the
zones that offer each. A type keeps what the candidate filters and the allocatable model need: its vCPUs and nominal
memory, its CPU architecture, whether it bursts or has GPUs, the zones it is offered in, the disks it takes, its CSI
driver's attach slots and, on AWS, the VPC CNI's max pods.
"""

from dataclasses import dataclass
from typing import Dict, FrozenSet, Mapping, Optional, Protocol, Sequence

from zoorik.cloud.aws import NITRO_ATTACHMENTS, AwsCli, InstanceType
from zoorik.cloud.azure import AzureCli, ComputeSku
from zoorik.format import MIB_PER_GIB

AZURE_DISK = "disk.csi.azure.com"
EBS = "ebs.csi.aws.com"
# The kubernetes.io/arch value of each cloud's CPU architecture name; a type of any other architecture is left out.
AZURE_ARCHITECTURES = {"x64": "amd64", "Arm64": "arm64"}
AWS_ARCHITECTURES = {"x86_64": "amd64", "arm64": "arm64"}
# How a driver's slots are counted, which says what each node holds back from them: an Azure node holds back the
# same slots on every size; an EBS slot on a Nitro type without a dedicated limit is shared with network interfaces.
DATA_DISKS = "data disks"
SHARED = "shared with network interfaces"
OWN = "its own"
# The EBS CSI driver's slots on an older (Xen) type, when the type lists no lower limit; a Nitro type's are its
# attachments.
XEN_SLOTS = 39
# EBS volumes are NVMe devices on Nitro types only.
NVME_REQUIRED = "required"
UNSUPPORTED = "unsupported"
DEDICATED = "dedicated"
# Prefix delegation gives each address slot of an interface 16 addresses.
PREFIX_ADDRESSES = 16


@dataclass(frozen=True)
class Slots:
    """A CSI driver's attach slots on a type, before what each node holds back, and how the driver counts them."""

    kind: str
    count: int


@dataclass(frozen=True)
class EniPods:
    """The VPC CNI's max pods on an AWS type: from its interfaces' addresses, and with prefix delegation."""

    standard: int
    prefix: int


@dataclass(frozen=True)
class MachineType:
    """A machine type a region offers: its size, its kind, and where and with which disks the account may run it."""

    name: str
    vcpus: int
    # Its nominal memory, as the cloud lists it.
    memory_mib: int
    # As the kubernetes.io/arch label names it.
    architecture: str
    burstable: bool
    gpus: int
    # The zones it is offered in, as the topology.kubernetes.io/zone label names them.
    zones: FrozenSet[str]
    # Offered in the region outside its zones, to a machine in no zone.
    regional: bool
    # It takes Premium SSD, Premium SSD v2 and Ultra Disk.
    premium_io: bool
    # The zones where it takes Ultra Disk, and whether it does outside its zones.
    ultra_zones: FrozenSet[str]
    ultra_regional: bool
    # By CSI driver.
    attach: Mapping[str, Slots]
    # None on Azure, where the agent pool sets max pods.
    pods: Optional[EniPods]


class MachineCatalogue(Protocol):
    """A cloud's machine types, region by region."""

    def types(self, account: str, region: str) -> Mapping[str, MachineType]:
        """Give the machine types the region offers the subscription or account, by name in lowercase."""


class AzureCatalogue:
    """Azure's VM sizes, from the Compute SKUs of each subscription and region."""

    def __init__(self, cli: AzureCli) -> None:
        """Read the Compute SKUs through cli."""
        self._cli = cli

    def types(self, account: str, region: str) -> Mapping[str, MachineType]:
        """Give the VM sizes the subscription may use in the region; a size that doesn't list its size is left out."""
        sizes = self._cli.compute_skus(account, region).vm_sizes
        found: Dict[str, MachineType] = {}
        for key, sku in sizes.items():
            kind = azure_type(region, sku)
            if kind is not None:
                found[key] = kind
        return found


def azure_zone(region: str, zone: str) -> str:
    """Give an Azure zone as an AKS node's zone label names it, such as westeurope-1."""
    return f"{region}-{zone}"


def azure_type(region: str, sku: ComputeSku) -> Optional[MachineType]:
    """Give a VM size as step 3 compares it; None when its SKU lacks its vCPUs, memory or a known architecture."""
    vcpus, memory = sku.vcpus, sku.memory_gb
    architecture = None if sku.architecture is None else AZURE_ARCHITECTURES.get(sku.architecture)
    if vcpus is None or memory is None or architecture is None:
        return None
    disks = sku.max_data_disks
    return MachineType(
        name=sku.name,
        vcpus=vcpus,
        memory_mib=int(memory * MIB_PER_GIB),
        architecture=architecture,
        burstable=sku.burstable,
        gpus=sku.gpus,
        zones=frozenset(azure_zone(region, zone) for zone in sku.zones),
        regional=sku.offered,
        premium_io=sku.premium_io is True,
        ultra_zones=frozenset(azure_zone(region, zone) for zone in sku.ultra_ssd_zones),
        ultra_regional=sku.ultra_ssd,
        attach={} if disks is None else {AZURE_DISK: Slots(DATA_DISKS, disks)},
        pods=None,
    )


class AwsCatalogue:
    """AWS's instance types, from each region's instance types and the zones that offer each."""

    def __init__(self, cli: AwsCli) -> None:
        """Read the instance types through cli."""
        self._cli = cli

    def types(self, account: str, region: str) -> Mapping[str, MachineType]:
        """Give the region's instance types of a known architecture, each with the zones that offer it."""
        zones = self._cli.type_zones(region)
        found: Dict[str, MachineType] = {}
        for name, kind in self._cli.instance_types(region).items():
            architecture = aws_architecture(kind.architectures)
            if architecture is not None:
                found[name.lower()] = aws_type(kind, architecture, zones.get(name, ()))
        return found


def aws_architecture(architectures: Sequence[str]) -> Optional[str]:
    """Give an instance type's architecture as the kubernetes.io/arch label names it; None for a Mac type."""
    return next((AWS_ARCHITECTURES[name] for name in architectures if name in AWS_ARCHITECTURES), None)


def aws_type(kind: InstanceType, architecture: str, zones: Sequence[str]) -> MachineType:
    """Give an instance type as step 3 compares it."""
    addresses = kind.max_interfaces * (kind.ipv4_per_interface - 1)
    return MachineType(
        name=kind.name,
        vcpus=kind.vcpus,
        memory_mib=kind.memory_mib,
        architecture=architecture,
        burstable=kind.burstable,
        gpus=kind.gpus,
        zones=frozenset(zones),
        regional=False,
        premium_io=True,
        ultra_zones=frozenset(),
        ultra_regional=False,
        attach={EBS: ebs_slots(kind)},
        pods=EniPods(addresses + 2, addresses * PREFIX_ADDRESSES + 2),
    )


def ebs_slots(kind: InstanceType) -> Slots:
    """Give the EBS CSI driver's slots on an instance type, before what each node holds back.

    A type with a dedicated limit has that many. Any other type has 28 slots on Nitro and 39 on Xen, or its listed limit
    when that is lower; on Nitro its NVMe instance-store disks and GPUs take slots, and so do its network interfaces,
    which a node holds back, all but the first where the type lists a limit.
    """
    listed = kind.max_attachments
    if listed is not None and kind.attachment_limit is not None and kind.attachment_limit.lower() == DEDICATED:
        return Slots(OWN, listed)
    nitro = kind.ebs_nvme == NVME_REQUIRED
    slots = NITRO_ATTACHMENTS if nitro else XEN_SLOTS
    if listed is not None:
        slots = min(slots, listed)
    if not nitro:
        return Slots(OWN, slots)
    store = 0 if kind.store_nvme in (None, UNSUPPORTED) else kind.store_disks
    first_interface = 0 if listed is None else 1
    return Slots(SHARED, slots - store - kind.gpus + first_interface)
