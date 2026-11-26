"""What each VM's size allows its data disks, as the disk method counts them.

Azure's limits come from the Compute SKUs of the VM's subscription and region; AWS's from the instance types of its
region.
"""

import re
from typing import Optional, Sequence

from zoorik.cloud.aws import NITRO_ATTACHMENTS, Ec2Instance, InstanceType
from zoorik.cloud.azure import RegionSkus
from zoorik.disks.model import AzureLimits, DataDisk, EbsLimits, Vm

PREMIUM_V2 = "PremiumV2_LRS"
# Regions where Azure takes Premium SSD v2 on a VM without an availability zone (nonzonal disks), from Microsoft Learn,
# "Deploy a Premium SSD v2 managed disk", Regional availability (updated 10 Sept 2026). The Compute SKUs list only
# the zones that offer Premium SSD v2, not this, so a region missing here gets Premium SSD.
V2_NONZONAL_REGIONS = frozenset(
    (
        "australiacentral2",
        "australiasoutheast",
        "brazilsoutheast",
        "canadaeast",
        "germanynorth",
        "northcentralus",
        "norwaywest",
        "southindia",
        "switzerlandwest",
        "taiwannorth",
        "ukwest",
        "usgovarizona",
        "westcentralus",
        "westus",
        "australiaeast",
        "brazilsouth",
        "eastasia",
        "francecentral",
        "germanywestcentral",
        "italynorth",
        "koreacentral",
        "mexicocentral",
        "newzealandnorth",
        "norwayeast",
        "polandcentral",
        "spaincentral",
        "southafricanorth",
        "southeastasia",
        "swedencentral",
        "switzerlandnorth",
    )
)
# EBS slots as the disk method counts them: the device names /dev/sdf to /dev/sdz, within the type's attachments.
EBS_DEVICE_NAMES = 21
SLOT_DEVICE = re.compile(r"/dev/(?:sd|xvd)([f-z])")


def azure_limits(vm: Vm, skus: RegionSkus) -> Optional[AzureLimits]:
    """Give what the VM's size allows in its region; None when the region's SKUs don't list the size's data disks."""
    sku = skus.vm_sizes.get(vm.size.lower())
    slots = None if sku is None else sku.max_data_disks
    if sku is None or slots is None:
        return None
    return AzureLimits(slots, positive(sku.uncached_iops), positive(sku.uncached_mbps), v2_offered(vm, skus))


def v2_offered(vm: Vm, skus: RegionSkus) -> bool:
    """Tell whether the VM can take Premium SSD v2 disks: in its zone, or, without a zone, in its region."""
    v2 = skus.disk_types.get(PREMIUM_V2)
    if v2 is None:
        return False
    if vm.zone is not None:
        return vm.zone in v2.zones
    return v2.offered and vm.region in V2_NONZONAL_REGIONS


def ebs_limits(instance: Ec2Instance, data: Sequence[DataDisk], kind: InstanceType) -> EbsLimits:
    """Give the instance's data-volume slots and EBS limits.

    The slots are the type's attachment limit less the root volume and, where the limit is shared, the network
    interfaces and NVMe instance-store disks, within the device names /dev/sdf to /dev/sdz.
    """
    limit = kind.max_attachments
    shared = limit is None or (kind.attachment_limit is not None and kind.attachment_limit.lower() == "shared")
    store_disks = 0 if kind.store_nvme == "unsupported" else kind.store_disks
    others = 1 + (1 + store_disks if shared else 0)
    type_slots = max(0, min(EBS_DEVICE_NAMES, (NITRO_ATTACHMENTS if limit is None else limit) - others))
    interfaces = max(0, instance.interfaces - 1) if shared else 0
    held = [ord(match.group(1)) - ord("f") for match in (SLOT_DEVICE.fullmatch(disk.device) for disk in data) if match]
    free = max(0, min(type_slots - interfaces - len(data), EBS_DEVICE_NAMES - 1 - max(held, default=-1)))
    return EbsLimits(
        slots=len(data) + free,
        free_slots=free,
        uncached_iops=positive(kind.baseline_iops),
        uncached_mbps=positive(kind.baseline_mbps),
        nitro=kind.ebs_nvme == "required",
    )


def positive(figure: Optional[float]) -> Optional[int]:
    """Give a listed limit in whole units; None when it is not listed, or listed as none."""
    if figure is None or int(figure) <= 0:
        return None
    return int(figure)
