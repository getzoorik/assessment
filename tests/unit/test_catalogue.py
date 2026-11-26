"""The machine types a region offers, as step 3 compares them: Azure's VM sizes and AWS's instance types.

Each keeps its size, architecture, zones and disks; an AWS type also keeps the VPC CNI's max pods, and the EBS CSI
driver's slots follow the driver's rule.
"""

import unittest
from dataclasses import replace
from typing import Any, Mapping, Optional
from unittest import mock

from zoorik.cloud.aws import AwsCli, InstanceType
from zoorik.cloud.azure import AzureCli, ComputeSku, RegionSkus
from zoorik.kubernetes.catalogue import (
    AZURE_DISK,
    DATA_DISKS,
    EBS,
    OWN,
    SHARED,
    AwsCatalogue,
    AzureCatalogue,
    EniPods,
    MachineType,
    Slots,
    aws_architecture,
    aws_type,
    azure_type,
    ebs_slots,
)

REGION = "westeurope"
SIZE_CAPABILITIES = {
    "vCPUs": "4",
    "MemoryGB": "16",
    "CpuArchitectureType": "x64",
    "PremiumIO": "True",
    "MaxDataDiskCount": "8",
    "UltraSSDAvailable": "True",
}


def size(capabilities: Mapping[str, str], **changes: Any) -> ComputeSku:
    """Make the VM size Standard_D4s_v5 with capabilities, offered in zones 1, 2 and 3 and with Ultra Disk in zone 1."""
    made = ComputeSku(
        resource_type="virtualMachines",
        name="Standard_D4s_v5",
        family="standardDSv5Family",
        capabilities=capabilities,
        zones=frozenset({"1", "2", "3"}),
        offered=True,
        ultra_ssd_zones=frozenset({"1"}),
    )
    return replace(made, **changes)


def instance_type(name: str = "m6i.large", **changes: Any) -> InstanceType:
    """Make a Nitro instance type of 2 vCPUs and 8 GiB, 3 interfaces of 10 addresses each, and no listed EBS limit."""
    made = InstanceType(
        name=name,
        vcpus=2,
        memory_mib=8192,
        architectures=("x86_64",),
        burstable=False,
        gpus=0,
        max_attachments=None,
        attachment_limit=None,
        ebs_nvme="required",
        baseline_iops=None,
        baseline_mbps=None,
        store_disks=0,
        store_nvme=None,
        max_interfaces=3,
        ipv4_per_interface=10,
    )
    return replace(made, **changes)


class AzureTypeTest(unittest.TestCase):
    """A VM size as step 3 compares it."""

    def test_a_size_keeps_its_zones_disks_and_ultra_disk_as_aks_names_them(self) -> None:
        """Zones become AKS zone labels; Premium SSD, Ultra Disk and the data-disk slots come from its capabilities."""
        self.assertEqual(
            azure_type(REGION, size(SIZE_CAPABILITIES)),
            MachineType(
                name="Standard_D4s_v5",
                vcpus=4,
                memory_mib=16384,
                architecture="amd64",
                burstable=False,
                gpus=0,
                zones=frozenset({"westeurope-1", "westeurope-2", "westeurope-3"}),
                regional=True,
                premium_io=True,
                ultra_zones=frozenset({"westeurope-1"}),
                ultra_regional=True,
                attach={AZURE_DISK: Slots(DATA_DISKS, 8)},
                pods=None,
            ),
        )

    def test_a_restricted_size_is_offered_nowhere(self) -> None:
        """A size the subscription may not use lists no zones and isn't offered in the region."""
        kind = azure_type(REGION, size(SIZE_CAPABILITIES, zones=frozenset(), offered=False))
        assert kind is not None
        self.assertEqual((kind.zones, kind.regional), (frozenset(), False))

    def test_a_size_without_its_figures_or_of_another_architecture_is_left_out(self) -> None:
        """A size that lacks its vCPUs, its memory or a known CPU architecture gives no type."""
        for left_out in ("vCPUs", "MemoryGB", "CpuArchitectureType"):
            with self.subTest(left_out):
                capabilities = {key: value for key, value in SIZE_CAPABILITIES.items() if key != left_out}
                self.assertIsNone(azure_type(REGION, size(capabilities)))
        self.assertIsNone(azure_type(REGION, size({**SIZE_CAPABILITIES, "CpuArchitectureType": "Other"})))

    def test_the_catalogue_keeps_the_sizes_that_give_their_figures(self) -> None:
        """The subscription's sizes in the region, by name in lowercase, without one that lacks its vCPUs."""
        cli = mock.create_autospec(AzureCli, instance=True)
        partial = size({"MemoryGB": "16"}, name="Standard_X1")
        cli.compute_skus.return_value = RegionSkus(
            {"standard_d4s_v5": size(SIZE_CAPABILITIES), "standard_x1": partial}, {}
        )
        found = AzureCatalogue(cli).types("sub-one", REGION)
        self.assertEqual(sorted(found), ["standard_d4s_v5"])
        cli.compute_skus.assert_called_once_with("sub-one", REGION)


class AwsTypeTest(unittest.TestCase):
    """An instance type as step 3 compares it, with the VPC CNI's max pods and the EBS CSI driver's slots."""

    def test_a_type_keeps_its_zones_and_the_vpc_cnis_max_pods(self) -> None:
        """3 interfaces of 10 addresses give 3 x 9 + 2 = 29 pods, and 3 x 9 x 16 + 2 = 434 with prefix delegation."""
        kind = aws_type(instance_type(), "amd64", ("eu-west-1a", "eu-west-1b"))
        self.assertEqual(kind.pods, EniPods(29, 434))
        self.assertEqual((kind.zones, kind.regional), (frozenset({"eu-west-1a", "eu-west-1b"}), False))
        self.assertEqual(kind.attach, {EBS: Slots(SHARED, 28)})

    def test_ebs_slots_follow_the_drivers_rule(self) -> None:
        """A dedicated limit is the type's own; Nitro shares 28 slots, Xen has 39, and a listed lower limit wins.

        On Nitro, NVMe instance-store disks and GPUs take slots, and a listed limit leaves out the first interface.
        """
        cases = {
            "dedicated": (instance_type(max_attachments=128, attachment_limit="dedicated"), Slots(OWN, 128)),
            "Nitro": (instance_type(), Slots(SHARED, 28)),
            "Nitro with a listed limit": (
                instance_type(max_attachments=27, attachment_limit="shared"),
                Slots(SHARED, 28),
            ),
            "Nitro with instance store and a GPU": (
                instance_type(store_disks=2, store_nvme="required", gpus=1),
                Slots(SHARED, 25),
            ),
            "Xen": (instance_type(ebs_nvme="unsupported"), Slots(OWN, 39)),
            "Xen with a listed limit": (instance_type(ebs_nvme="unsupported", max_attachments=20), Slots(OWN, 20)),
        }
        for name, (kind, slots) in cases.items():
            with self.subTest(name):
                self.assertEqual(ebs_slots(kind), slots)

    def test_architectures_are_named_as_the_kubernetes_label_names_them(self) -> None:
        """x86_64 is amd64 and arm64 stays; a Mac type has no architecture a node runs."""
        cases: Mapping[str, Optional[str]] = {"x86_64": "amd64", "arm64": "arm64", "x86_64_mac": None}
        for name, expected in cases.items():
            with self.subTest(name):
                self.assertEqual(aws_architecture((name,)), expected)

    def test_the_catalogue_joins_each_type_to_its_zones(self) -> None:
        """Types are kept by name in lowercase with the zones that offer them; a Mac type is left out."""
        cli = mock.create_autospec(AwsCli, instance=True)
        mac = instance_type("mac2.metal", architectures=("arm64_mac",))
        cli.instance_types.return_value = {"m6i.large": instance_type(), "mac2.metal": mac}
        cli.type_zones.return_value = {"m6i.large": ("eu-west-1a",)}
        found = AwsCatalogue(cli).types("111122223333", "eu-west-1")
        self.assertEqual(sorted(found), ["m6i.large"])
        self.assertEqual(found["m6i.large"].zones, frozenset({"eu-west-1a"}))
