"""The VMs and instances with data disks: Azure's through Resource Graph, AWS's region by region."""

from typing import List, Sequence, Tuple

from tests.replay.disks import (
    D8S,
    SKUS_ARGS,
    SSM_ONLINE_ARGS,
    attached,
    azure_inventory,
    disk_row,
    graph_args,
    region_reads,
    root_volume,
    skus,
    ssm_online,
    types_call,
    vm_row,
    volume,
)
from tests.replay.recording import Call
from tests.replay.scenarios import (
    AWS_SCOPE,
    AZURE_SCOPE,
    EU,
    SUB,
    Json,
    aws_failure,
    az_call,
    az_failure,
    cluster_list,
    instance,
    instances_args,
)
from tests.unit.clis import CloudTestCase, WatchingRunner
from zoorik.cloud import azure as azure_cloud
from zoorik.cloud.aws import AwsCli
from zoorik.cloud.azure import AzureCli
from zoorik.cloud.keys import AWS, AZURE
from zoorik.cloud.started import StartedCommands
from zoorik.disks.aws_inventory import NO_SSM, NO_VOLUME, AwsInventory
from zoorik.disks.azure_inventory import NO_RECORD, NO_SIZE, NOT_READABLE, UNMANAGED, AzureInventory
from zoorik.disks.model import Inventory, NotAssessed, ScopeRead, Unread

REFUSED_AZURE = "ERROR: (AuthorizationFailed) The client does not have authorization to perform this action.\n"
REFUSED_AWS = "An error occurred (UnauthorizedOperation) when calling the operation: not authorized.\n"


def ignore(done: int, total: int) -> None:
    """Take a read's progress, which these tests don't check."""


class AzureInventoryTest(CloudTestCase):
    """AzureInventory."""

    def read(self, calls: Sequence[Call]) -> Tuple[Inventory, WatchingRunner]:
        """Read the subscription's VMs from calls."""
        host, runner = self.host(calls)
        return AzureInventory(AzureCli(host, StartedCommands()), AZURE_SCOPE).read(ignore), runner

    def test_a_subscription_resource_graph_cannot_read_is_named_and_read_no_further(self) -> None:
        """A subscription missing from Resource Graph's answer has no Reader access there."""
        refused = az_call(graph_args(azure_cloud.SUBSCRIPTIONS_QUERY), {"data": []})
        inventory, runner = self.read([refused])
        self.assertEqual(inventory.unread, (Unread(AZURE, "subscription-one", NOT_READABLE),))
        self.assertEqual((inventory.vms, inventory.read, len(runner.runs)), ((), (), 1))

    def test_a_failed_query_names_the_subscriptions_it_covers(self) -> None:
        """The disk query fails, so the subscription is named with the reason."""
        calls = [*azure_inventory([], [])[:1], az_failure(graph_args(azure_cloud.DISK_QUERY), REFUSED_AZURE)]
        inventory, _ = self.read(calls)
        self.assertEqual(inventory.unread, (Unread(AZURE, "subscription-one", "Resource Graph: access denied"),))

    def test_a_disk_takes_its_size_only_from_its_own_record(self) -> None:
        """A managed disk's own record gives its size; a VHD's comes from the VM; a missing record or size is named."""
        vhd: Json = {
            "lun": 1,
            "name": "vhd-one",
            "vhd": {"uri": "https://store.example.test/one.vhd"},
            "diskSizeGB": 128,
        }
        data = [attached(0, "data-one", 512), vhd, attached(2, "data-two", 512), attached(3, "data-gone", 512)]
        disks = [disk_row("data-one", 1024, "P30"), {**disk_row("data-two", 512, "P20"), "sizeGb": None}]
        calls = [*azure_inventory([vm_row("vm-one", data), vm_row("vm-empty", [])], disks), skus()]
        inventory, _ = self.read(calls)
        self.assertEqual(inventory.read, (ScopeRead("subscription-one", 2, 2),))
        (vm,) = inventory.vms
        self.assertEqual(
            [(disk.lun, disk.size_gb, disk.note) for disk in vm.data],
            [
                (0, 1024, ""),
                (1, 128, UNMANAGED),
                (2, None, NO_SIZE),
                (3, None, NO_RECORD),
            ],
        )

    def test_a_managed_by_that_names_no_resource_type_names_every_vm(self) -> None:
        """No VM can be told apart from a cluster node, so each is named instead of assessed."""
        managed: Json = {"subscriptionId": SUB, "name": "group-two", "managedBy": "not-a-resource-id"}
        inventory, _ = self.read(azure_inventory([vm_row("vm-one", [attached(0, "data-one", 512)])], [], [managed]))
        reason = "could not tell which VMs are cluster nodes: unexpected answer: a resource group's managedBy names no "
        self.assertEqual(
            inventory.not_assessed, (NotAssessed(AZURE, SUB, "westeurope", "vm-one", reason + "resource type"),)
        )

    def test_limits_that_cannot_be_read_are_named(self) -> None:
        """A size the region doesn't list, a capability not of its form, and SKUs that can't be read, each say so."""
        vms = [
            vm_row("vm-one", [attached(0, "data-one", 512)]),
            vm_row("vm-big", [attached(0, "data-two", 512)], size="Standard_E4s_v5"),
        ]
        malformed = {**D8S, "capabilities": [{"name": "MaxDataDiskCount", "value": "sixteen"}]}
        cases: List[Tuple[Call, List[str]]] = [
            (skus(), ["", "Standard_E4s_v5 not listed in westeurope"]),
            (
                skus(malformed),
                ["unexpected answer: the MaxDataDiskCount capability of Standard_D8s_v5 is not of its form"],
            ),
            (az_failure(SKUS_ARGS, REFUSED_AZURE), ["access denied", "access denied"]),
        ]
        for answer, notes in cases:
            with self.subTest(notes):
                inventory, _ = self.read([*azure_inventory(vms[: len(notes)], []), answer])
                self.assertEqual([vm.facts_note for vm in inventory.vms], notes)


class AwsInventoryTest(CloudTestCase):
    """AwsInventory."""

    def read(self, calls: Sequence[Call]) -> Tuple[Inventory, WatchingRunner]:
        """Read the region's instances from calls."""
        host, runner = self.host(calls)
        return AwsInventory(AwsCli(host, StartedCommands()), AWS_SCOPE).read(ignore), runner

    def test_an_instance_with_only_its_root_volume_is_counted_not_listed(self) -> None:
        """No data volume: nothing more is read for it."""
        inventory, runner = self.read(region_reads(EU, [instance(1, "m6i.2xlarge", [])], [root_volume(1)]))
        self.assertEqual((inventory.vms, inventory.read, len(runner.runs)), ((), (ScopeRead(EU, 1, 1),), 2))

    def test_data_volumes_are_numbered_in_device_name_order(self) -> None:
        """A volume the region's list doesn't hold is named."""
        item = instance(1, "m6i.2xlarge", [("/dev/sdg", "vol-02"), ("/dev/sdf", "vol-01"), ("/dev/sdh", "vol-03")])
        volumes = [volume("vol-01", "gp2", 500, 1500), volume("vol-02", "gp3", 100, 3000, 125)]
        inventory, _ = self.read([*region_reads(EU, [item], volumes), types_call(), ssm_online(["i-01"])])
        (vm,) = inventory.vms
        self.assertEqual(
            [(disk.lun, disk.device, disk.size_gb, disk.note) for disk in vm.data],
            [(0, "/dev/sdf", 500, ""), (1, "/dev/sdg", 100, ""), (2, "/dev/sdh", None, NO_VOLUME)],
        )
        self.assertEqual((vm.name, vm.os_type, vm.power, vm.channel), ("i-01", "Linux", "Running", None))

    def test_limits_and_systems_manager_that_cannot_be_read_are_named(self) -> None:
        """The instance types and Systems Manager each fail: every instance says why."""
        item = instance(1, "m6i.2xlarge", [("/dev/sdf", "vol-01")])
        calls = [
            *region_reads(EU, [item], [volume("vol-01", "gp2", 500, 1500)]),
            aws_failure(["ec2", "describe-instance-types", "--region", EU], REFUSED_AWS),
            aws_failure(SSM_ONLINE_ARGS, REFUSED_AWS),
        ]
        (vm,) = self.read(calls)[0].vms
        self.assertEqual((vm.limits, vm.facts_note), (None, "instance type limits not readable: access denied"))
        self.assertEqual(vm.channel, "Systems Manager not readable: access denied")

    def test_a_type_the_region_doesnt_list_and_an_instance_systems_manager_doesnt_reach_are_named(self) -> None:
        """The type is not listed; the instance's agent is not online."""
        item = instance(1, "c7g.large", [("/dev/sdf", "vol-01")])
        calls = [*region_reads(EU, [item], [volume("vol-01", "gp2", 500, 1500)]), types_call(), ssm_online([])]
        (vm,) = self.read(calls)[0].vms
        self.assertEqual((vm.facts_note, vm.channel), ("c7g.large not listed in eu-west-1", NO_SSM))

    def test_windows_instances_ask_nothing_of_systems_manager(self) -> None:
        """Only a Linux instance is read inside, so a region of Windows instances reads no agent list."""
        item = {**instance(1, "m6i.2xlarge", [("/dev/sdf", "vol-01")]), "Platform": "windows"}
        inventory, runner = self.read([*region_reads(EU, [item], [volume("vol-01", "gp2", 500, 1500)]), types_call()])
        self.assertEqual(([vm.os_type for vm in inventory.vms], len(runner.runs)), (["Windows"], 3))

    def test_a_region_that_fails_is_named(self) -> None:
        """The region's instances can't be read."""
        inventory, _ = self.read([aws_failure(instances_args(EU), REFUSED_AWS)])
        self.assertEqual(inventory.unread, (Unread(AWS, EU, "access denied"),))

    def test_the_eks_cluster_list_is_read_once_for_a_region(self) -> None:
        """Two instances tagged as nodes of clusters EKS doesn't list: both kept, from the one list answer there is."""
        tags = [{"kubernetes.io/cluster/own-one": "owned"}, {"kubernetes.io/cluster/own-two": "shared"}]
        items = [
            instance(number, "m6i.2xlarge", [("/dev/sdf", f"vol-0{number}")], tags[number - 1]) for number in (1, 2)
        ]
        volumes = [volume("vol-01", "gp2", 500, 1500), volume("vol-02", "gp2", 500, 1500)]
        calls = [*region_reads(EU, items, volumes), cluster_list(["cluster-one"]), types_call(), ssm_online(["i-01"])]
        inventory, _ = self.read(calls)
        self.assertEqual([(vm.name, vm.channel) for vm in inventory.vms], [("i-01", None), ("i-02", NO_SSM)])
