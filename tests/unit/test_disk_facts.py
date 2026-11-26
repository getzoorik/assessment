"""What each VM's size allows its data disks: Azure's from the Compute SKUs, AWS's from the instance types."""

import dataclasses
import unittest
from typing import Mapping, Optional, Tuple

from zoorik.cloud.aws import AttachedVolume, Ec2Instance, InstanceType
from zoorik.cloud.azure import ComputeSku, RegionSkus
from zoorik.cloud.keys import AZURE
from zoorik.disks.facts import azure_limits, ebs_limits, positive, v2_offered
from zoorik.disks.model import AzureLimits, DataDisk, EbsLimits, Vm


def size(capabilities: Mapping[str, str]) -> ComputeSku:
    """Make the Compute SKU of the VM size Standard_D8s_v5, with its capabilities."""
    return ComputeSku("virtualMachines", "Standard_D8s_v5", None, capabilities, frozenset(("1", "2", "3")), True, False)


def premium_v2(zones: Tuple[str, ...] = ("1", "2", "3"), offered: bool = True) -> ComputeSku:
    """Make the Compute SKU of Premium SSD v2, offered in zones, or in none of the region where offered is False."""
    return ComputeSku("disks", "PremiumV2_LRS", None, {}, frozenset(zones), offered, False)


D8S = size({"MaxDataDiskCount": "16", "UncachedDiskIOPS": "12800", "UncachedDiskBytesPerSecond": "290000000"})


def region(*sizes: ComputeSku, v2: Optional[ComputeSku] = None) -> RegionSkus:
    """Make a region's SKUs: the VM sizes by their lowercase names, and Premium SSD v2 when given."""
    return RegionSkus({listed.name.lower(): listed for listed in sizes}, {} if v2 is None else {v2.name: v2})


def vm(kind: str = "Standard_D8s_v5", zone: Optional[str] = "1", where: str = "westeurope") -> Vm:
    """Make an Azure VM of a size, in a zone of a region."""
    return Vm("vm", "vm-one", AZURE, "sub-one", where, zone, kind, "Linux", "Running", (), None, None, None, "")


class AzureLimitsTest(unittest.TestCase):
    """azure_limits and v2_offered."""

    def test_a_size_listed_in_the_region_gives_its_slots_and_speeds(self) -> None:
        """The size is found whatever the case of its name; bytes a second become MB/s of 10^6 bytes."""
        expected = AzureLimits(slots=16, uncached_iops=12800, uncached_mbps=290, v2=True)
        self.assertEqual(azure_limits(vm(), region(D8S, v2=premium_v2())), expected)
        self.assertEqual(azure_limits(vm("STANDARD_D8S_V5"), region(D8S, v2=premium_v2())), expected)

    def test_a_size_with_no_listed_slots_has_no_limits(self) -> None:
        """A size the region doesn't list, or lists without MaxDataDiskCount, gives None."""
        self.assertIsNone(azure_limits(vm("Standard_E4s_v5"), region(D8S)))
        self.assertIsNone(azure_limits(vm(), region(size({"UncachedDiskIOPS": "12800"}))))

    def test_a_speed_listed_as_zero_is_not_known(self) -> None:
        """An uncached limit of 0 is no limit at all."""
        listed = size({"MaxDataDiskCount": "16", "UncachedDiskIOPS": "0"})
        self.assertEqual(azure_limits(vm(), region(listed)), AzureLimits(16, None, None, False))

    def test_premium_ssd_v2_goes_where_the_vm_runs(self) -> None:
        """In the VM's zone; without a zone, only in the regions that take nonzonal disks, where it is offered."""
        self.assertTrue(v2_offered(vm(zone="1"), region(v2=premium_v2(("1", "2")))))
        self.assertFalse(v2_offered(vm(zone="3"), region(v2=premium_v2(("1", "2")))))
        self.assertTrue(v2_offered(vm(zone=None, where="westus"), region(v2=premium_v2(()))))
        self.assertFalse(v2_offered(vm(zone=None, where="westeurope"), region(v2=premium_v2(()))))
        self.assertFalse(v2_offered(vm(zone=None, where="westus"), region(v2=premium_v2((), offered=False))))
        self.assertFalse(v2_offered(vm(), region()))


def instance(devices: Tuple[str, ...], interfaces: int = 1) -> Tuple[Ec2Instance, Tuple[DataDisk, ...]]:
    """Make an instance with data volumes at devices, and its data disks."""
    attached = tuple(AttachedVolume(device, f"vol-{index}") for index, device in enumerate(devices))
    record = Ec2Instance("i-01", "m6i.2xlarge", "running", None, None, None, "/dev/xvda", attached, interfaces)
    return record, tuple(DataDisk(index, 100, None, "", device) for index, device in enumerate(devices))


NITRO = InstanceType(
    name="m6i.2xlarge",
    vcpus=8,
    memory_mib=32768,
    architectures=("x86_64",),
    burstable=False,
    gpus=0,
    max_attachments=28,
    attachment_limit="shared",
    ebs_nvme="required",
    baseline_iops=12000,
    baseline_mbps=312.5,
    store_disks=0,
    store_nvme=None,
    max_interfaces=4,
    ipv4_per_interface=15,
)


class EbsLimitsTest(unittest.TestCase):
    """ebs_limits."""

    def test_a_shared_limit_leaves_the_root_volume_and_one_interface_out(self) -> None:
        """28 shared less the root volume and one interface is 26, within the 21 names /dev/sdf to /dev/sdz."""
        record, data = instance(("/dev/sdf",))
        expected = EbsLimits(slots=21, free_slots=20, uncached_iops=12000, uncached_mbps=312, nitro=True)
        self.assertEqual(ebs_limits(record, data, NITRO), expected)

    def test_more_interfaces_and_nvme_instance_storage_take_shared_slots(self) -> None:
        """Two more interfaces and two NVMe instance-store disks: 28 - 4 = 24 slots, 21 names, less 2 and 1 held."""
        record, data = instance(("/dev/sdf",), interfaces=3)
        kind = dataclasses.replace(NITRO, store_disks=2, store_nvme="required")
        self.assertEqual(ebs_limits(record, data, kind).free_slots, 18)

    def test_a_dedicated_limit_counts_only_the_root_volume(self) -> None:
        """A dedicated limit of 32 leaves interfaces out: the 21 names, less the one data volume."""
        record, data = instance(("/dev/sdf",), interfaces=3)
        kind = dataclasses.replace(NITRO, max_attachments=32, attachment_limit="dedicated")
        self.assertEqual(ebs_limits(record, data, kind).free_slots, 20)

    def test_a_type_that_lists_no_limit_takes_the_shared_nitro_limit(self) -> None:
        """No listed limit counts as 28 shared."""
        record, data = instance(("/dev/sdf",))
        kind = dataclasses.replace(NITRO, max_attachments=None, attachment_limit=None)
        self.assertEqual(ebs_limits(record, data, kind).free_slots, 20)

    def test_device_names_past_the_last_volume_are_the_free_slots(self) -> None:
        """A volume at /dev/sdz leaves no name after it; Xen types attach volumes without NVMe."""
        record, data = instance(("/dev/sdz",))
        kind = dataclasses.replace(NITRO, ebs_nvme="unsupported")
        self.assertEqual(ebs_limits(record, data, kind), EbsLimits(1, 0, 12000, 312, False))

    def test_a_listed_limit_is_given_in_whole_units(self) -> None:
        """312.5 MB/s is 312; none listed, or 0, is not known."""
        self.assertEqual([positive(312.5), positive(None), positive(0)], [312, None, None])
