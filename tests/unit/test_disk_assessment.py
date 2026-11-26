"""The disk part's judgement: what rules a pool out, what a pool saves, and what the data disks cost today."""

import unittest
from dataclasses import replace
from typing import Mapping, Optional, Sequence, Tuple

from tests.replay.fake_cloud import CapturingConsole, VirtualClock
from tests.replay.replay import ReplayPrompt
from tests.replay.scenarios import FACTS
from zoorik.cloud.cli import Window
from zoorik.cloud.keys import AWS, AZURE
from zoorik.console import Progress
from zoorik.disks.assessment import (
    ENCRYPTION,
    NO_PRICE,
    NO_SLOTS,
    NOT_MOUNTED,
    SHARED,
    DiskCloud,
    DisksAssessment,
    Figures,
    activity_of,
    assess,
    cost_today,
    inventory_lines,
    screen,
    unreachable,
)
from zoorik.disks.aws_inventory import NO_SSM
from zoorik.disks.model import (
    Activity,
    AzureLimits,
    DataDisk,
    Disk,
    EbsLimits,
    Guest,
    Hourly,
    Inside,
    Inventory,
    Load,
    NotAssessed,
    Price,
    ScopeRead,
    Unread,
    Vm,
    VmResult,
)
from zoorik.disks.pools import TOO_BUSY
from zoorik.errors import AccessDenied, PriceListUnreachable
from zoorik.pricing.disks import DiskSpec

GIB = 2**30
REGION = "westeurope"
KERNEL = "5.15.0-1057-azure"
# A VM size of 16 data-disk slots in no zone, so its pool goes on Premium SSD.
PREMIUM_SSD = AzureLimits(slots=16, uncached_iops=12800, uncached_mbps=290, v2=False)
CLUSTER_NODES = "could not tell which VMs are cluster nodes: access denied"
NO_LIMITS = "VM size limits not readable: access denied"
UNMEASURED = "metrics not readable: access denied"


def disk(name: str, region: str = REGION, *, shared: bool = False, ade: bool = False) -> Disk:
    """Make a 512 GB Premium SSD managed disk whose id is its name."""
    return Disk(name, AZURE, region, "Premium_LRS", 512, "", None, None, ade, shared)


def attached(lun: int, record: Optional[Disk], note: str = "") -> DataDisk:
    """Make a data disk at a LUN, with its record, or why it has none."""
    return DataDisk(lun, None if record is None else record.size_gb, record, note, "")


def vm(name: str, *data: DataDisk) -> Vm:
    """Make a running Linux VM of 16 slots in no zone, with data disks."""
    return Vm(
        f"/vms/{name}",
        name,
        AZURE,
        "sub-one",
        REGION,
        None,
        "Standard_D8s_v5",
        "Linux",
        "Running",
        data,
        None,
        None,
        PREMIUM_SSD,
        "",
    )


def guest(luns: Mapping[int, Tuple[str, ...]], used_gib: Mapping[str, int], kernel: str = KERNEL) -> Guest:
    """Make what the read inside a VM found: the mount points on each LUN, and each mount's used GiB."""
    return Guest(kernel, "module", luns, {mount: gib * GIB for mount, gib in used_gib.items()})


def busy(iops: float) -> Activity:
    """Make a disk's metrics: one hour at iops, half of them writes, and 10 MB/s."""
    return Activity({"10:00": Load(iops, 10e6, iops / 2, 5e6)}, "")


class DollarPerGib:
    """A price list that charges a dollar a month for each GiB of a pool's members."""

    def monthly(self, region: str, disk: DiskSpec) -> float:
        """Give the member's size in dollars."""
        return float(disk.size_gb)


def figures(prices: Mapping[str, Optional[float]], activity: Mapping[str, Activity]) -> Figures:
    """Make the disks' prices and metrics, by disk id; a disk with no price has the price list's reason."""
    listed = {
        name: Price(price, "" if price is not None else "no list price for E30 LRS in westeurope")
        for name, price in prices.items()
    }
    return Figures(listed, activity, DollarPerGib())


class CostTodayTest(unittest.TestCase):
    """cost_today."""

    def test_each_priced_disk_counts_once_and_rounds_half_up(self) -> None:
        """A disk two VMs share counts once; a VM with an unpriced disk adds its priced one; 20.50 rounds to 21."""
        shared = disk("shared-one", shared=True)
        vms = [
            vm("vm-one", attached(0, shared), attached(1, disk("data-one"))),
            vm("vm-two", attached(0, shared)),
            vm("vm-three", attached(0, disk("priced")), attached(1, disk("unpriced")), attached(2, None, "no record")),
        ]
        prices = {
            "shared-one": Price(70.0, ""),
            "data-one": Price(130.0, ""),
            "priced": Price(20.5, ""),
            "unpriced": Price(None, "no list price for E30 LRS in westeurope"),
        }
        self.assertEqual(cost_today(vms, prices), 70 + 130 + 21)


class InventoryLinesTest(unittest.TestCase):
    """inventory_lines."""

    def test_scopes_say_what_they_held_and_the_vms_left_out_are_counted_by_reason(self) -> None:
        """Scopes not read share a line for each reason; empty scopes are counted; plurals follow the counts."""
        unsure = [NotAssessed(AZURE, "sub-one", REGION, name, CLUSTER_NODES) for name in ("vm-a", "vm-b")]
        unread = [
            Unread(AZURE, "five", "access denied"),
            Unread(AZURE, "six", "access denied"),
            Unread(AZURE, "seven", "Resource Graph cannot read it (no Reader access?)"),
        ]
        read = [ScopeRead("one", 1, 2), ScopeRead("two", 0, 0), ScopeRead("three", 0, 0), ScopeRead("four", 3, 1)]
        inventory = Inventory((), tuple(unsure), tuple(read), tuple(unread), {})
        self.assertEqual(
            inventory_lines(AZURE, inventory),
            [
                "Subscription five, six: VMs not listed, access denied",
                "Subscription seven: VMs not listed, Resource Graph cannot read it (no Reader access?)",
                "Subscription one: 1 VM, 2 disks",
                "Subscription four: 3 VMs, 1 disk",
                "2 subscriptions with no VMs or disks.",
                f"2 VMs not assessed, {CLUSTER_NODES}",
            ],
        )

    def test_aws_lines_name_regions_instances_and_volumes(self) -> None:
        """One instance and one volume in a region, and one region with neither."""
        inventory = Inventory((), (), (ScopeRead("eu-west-1", 1, 1), ScopeRead("us-east-1", 0, 0)), (), {})
        self.assertEqual(
            inventory_lines(AWS, inventory),
            ["Region eu-west-1: 1 instance, 1 volume", "1 region with no instances or volumes."],
        )


class ScreenTest(unittest.TestCase):
    """screen and unreachable."""

    def test_what_the_cloud_reports_can_rule_a_pool_out(self) -> None:
        """Not Linux, run by another service, too few slots, or an older instance type."""
        xen = EbsLimits(slots=21, free_slots=20, uncached_iops=6000, uncached_mbps=93, nitro=False)
        cases = [
            (replace(vm("vm-one"), os_type="Windows"), "Windows VM: not estimated by this assessment yet"),
            (replace(vm("vm-one"), os_type=""), "Non-Linux VM: not estimated by this assessment yet"),
            (
                replace(vm("vm-one"), managed_by="Microsoft.Databricks/workspaces"),
                "run by another Azure service (Databricks): it is left to that service",
            ),
            (
                replace(vm("vm-one"), limits=AzureLimits(4, 3750, 85, True)),
                "too few data-disk slots (4): a pool needs 8 or more",
            ),
            (
                replace(vm("i-01"), cloud=AWS, size="m4.xlarge", limits=xen),
                "m4.xlarge is an older instance type: a pool needs a Nitro instance type",
            ),
        ]
        for machine, reason in cases:
            with self.subTest(reason):
                self.assertEqual(screen(machine), (reason,))
        self.assertEqual(screen(vm("vm-one")), ())

    def test_the_run_reads_inside_a_running_vm_with_a_disk_record_and_a_way_in(self) -> None:
        """A stopped VM, one with no disk record, or one with no way in says why; the rest are read."""
        one = attached(0, disk("data-one"))
        self.assertEqual(unreachable(replace(vm("vm-one", one), power="Deallocated")), "VM is not running")
        self.assertEqual(unreachable(vm("vm-one", attached(0, None, "unmanaged (VHD) disk"))), "no managed data disk")
        self.assertEqual(unreachable(replace(vm("i-01", attached(0, None)), cloud=AWS)), "data volumes not readable")
        self.assertEqual(unreachable(replace(vm("i-01", one), cloud=AWS, channel=NO_SSM)), NO_SSM)
        self.assertIsNone(unreachable(vm("vm-one", one)))


class AssessTest(unittest.TestCase):
    """assess: a VM's reasons, used space and saving."""

    def assess(
        self,
        machine: Vm,
        found: Optional[Guest],
        prices: Mapping[str, Optional[float]],
        activity: Mapping[str, Activity],
    ) -> VmResult:
        """Assess the VM after a read inside it that found found."""
        return assess(machine, Inside(found, ""), figures(prices, activity))

    def test_a_vm_its_cloud_rules_out_keeps_its_reasons_and_its_cost(self) -> None:
        """A Windows VM is not read inside, so it has no entry; its disks still cost what they cost."""
        machine = replace(vm("vm-one", attached(0, disk("data-one"))), os_type="Windows")
        result = assess(machine, None, figures({"data-one": 130.0}, {}))
        self.assertEqual(result.reasons, ("Windows VM: not estimated by this assessment yet",))
        self.assertEqual((result.current, result.used_gb, result.saving), (130.0, None, None))

    def test_a_vm_not_read_inside_says_why(self) -> None:
        """A read declined, or one that matched no data disk, gives no used space."""
        machine = vm("vm-one", attached(0, disk("data-one")))
        declined = assess(machine, Inside(None, "declined at the prompt"), figures({"data-one": 130.0}, {}))
        self.assertEqual(declined.reasons, ("disk usage not read: declined at the prompt",))
        unmatched = self.assess(machine, guest({}, {}), {"data-one": 130.0}, {})
        self.assertEqual(unmatched.reasons, ("disk usage not read: its data disks could not be matched inside the VM",))

    def test_what_the_read_finds_can_rule_a_pool_out_or_ask_for_work_first(self) -> None:
        """No btrfs rules it out; an old kernel and Azure Disk Encryption need work first, listed after."""
        machine = vm("vm-one", attached(0, disk("data-one", ade=True)))
        found = replace(guest({0: ("/data",)}, {"/data": 100}, "5.4.0-1109-azure"), btrfs="absent")
        result = self.assess(machine, found, {"data-one": 130.0}, {})
        self.assertEqual(
            result.reasons,
            (
                "no btrfs in its Linux kernel (5.4.0-1109-azure): a pool needs btrfs",
                "needs work first: upgrade its Linux kernel (5.4.0-1109-azure) to 5.14 or later",
                ENCRYPTION,
            ),
        )
        self.assertEqual((result.used_gb, result.saving), (100.0, None))

    def test_a_vm_with_no_mount_a_pool_could_take_says_why(self) -> None:
        """Every mount on a shared disk, or no data disk mounted."""
        shared = vm("vm-one", attached(0, disk("shared-one", shared=True)))
        result = self.assess(shared, guest({0: ("/shared",)}, {"/shared": 100}), {"shared-one": 70.0}, {})
        self.assertEqual(result.reasons, (SHARED,))
        bare = vm("vm-one", attached(0, disk("data-one")))
        self.assertEqual(self.assess(bare, guest({0: ()}, {}), {"data-one": 130.0}, {}).reasons, (NOT_MOUNTED,))

    def test_a_mount_whose_used_space_is_not_reported_is_named_and_not_sized(self) -> None:
        """The read printed no used figure for /logs, so the VM's used space is not known."""
        machine = vm("vm-one", attached(0, disk("data-one")), attached(1, disk("data-two")))
        found = guest({0: ("/data",), 1: ("/logs",)}, {"/data": 100})
        result = self.assess(machine, found, {"data-one": 130.0, "data-two": 130.0}, {})
        self.assertEqual(
            (result.reasons, result.used_gb, result.saving), (("used space on /logs not reported",), None, None)
        )

    def test_a_pool_needs_the_vms_limits_each_disks_price_and_its_activity(self) -> None:
        """Each missing figure stops the estimate with its reason."""
        found = guest({0: ("/data",)}, {"/data": 100})
        one = attached(0, disk("data-one"))
        unmeasured = {"data-one": activity_of(AccessDenied("access denied"))}
        cases: Sequence[Tuple[Vm, Mapping[str, Optional[float]], Mapping[str, Activity], str]] = [
            (replace(vm("vm-one", one), limits=None, facts_note="access denied"), {"data-one": 130.0}, {}, NO_LIMITS),
            (vm("vm-one", one), {"data-one": None}, {}, NO_PRICE),
            (vm("vm-one", one), {"data-one": 130.0}, unmeasured, "disk activity not read: " + UNMEASURED),
        ]
        for machine, prices, activity, reason in cases:
            with self.subTest(reason):
                result = self.assess(machine, found, prices, activity)
                self.assertEqual((result.reasons, result.used_gb, result.saving), ((reason,), 100.0, None))

    def test_too_few_free_slots_leave_no_room_for_the_pool(self) -> None:
        """Seven disks on an 8-slot VM leave one slot, which is held spare."""
        data = [attached(lun, disk(f"data-{lun}")) for lun in range(7)]
        machine = replace(vm("vm-one", *data), limits=AzureLimits(8, 12800, 290, False))
        prices = {f"data-{lun}": 130.0 for lun in range(7)}
        result = self.assess(machine, guest({0: ("/data",)}, {"/data": 100}), prices, {"data-0": busy(100)})
        self.assertEqual((result.reasons, result.saving), ((NO_SLOTS,), None))

    def test_the_busiest_mount_stays_on_its_disks_while_the_pool_cannot_carry_it(self) -> None:
        """Three 128 GiB Premium SSD members carry 1,500 IOPS: /busy's 5,000 stays; /quiet's pool saves 500 - 384."""
        machine = vm("vm-one", attached(0, disk("busy")), attached(1, disk("quiet")))
        found = guest({0: ("/busy",), 1: ("/quiet",)}, {"/busy": 100, "/quiet": 100})
        result = self.assess(machine, found, {"busy": 500.0, "quiet": 500.0}, {"busy": busy(5000), "quiet": busy(100)})
        saving = result.saving
        if saving is None:
            raise AssertionError(result.reasons)
        self.assertEqual([item.id for item in saving.pooled], ["quiet"])
        self.assertEqual((saving.today, saving.pool_gb, saving.pool_cost, saving.monthly), (500.0, 384, 384.0, 116.0))
        self.assertEqual(([item.id for item in saving.staying], saving.staying_cost), (["busy"], 500.0))
        self.assertEqual((result.reasons, result.used_gb, result.current), ((), 100.0, 1000.0))

    def test_a_pool_too_busy_for_its_one_mount_is_not_estimated(self) -> None:
        """No mount is left to stay behind, so the reason stands."""
        machine = vm("vm-one", attached(0, disk("busy")))
        result = self.assess(machine, guest({0: ("/busy",)}, {"/busy": 100}), {"busy": 500.0}, {"busy": busy(5000)})
        self.assertEqual((result.reasons, result.saving), ((TOO_BUSY,), None))

    def test_a_pool_that_costs_no_less_than_the_disks_saves_nothing(self) -> None:
        """Three 128 GiB members at $384 against a $100 disk."""
        machine = vm("vm-one", attached(0, disk("data-one")))
        result = self.assess(
            machine, guest({0: ("/data",)}, {"/data": 100}), {"data-one": 100.0}, {"data-one": busy(100)}
        )
        self.assertEqual(
            result.reasons, ("no saving: the pool would cost $384 a month, no less than these disks ($100)",)
        )
        self.assertEqual((result.used_gb, result.saving), (100.0, None))

    def test_a_saving_carries_the_vms_other_priced_disks_that_no_other_vm_shares(self) -> None:
        """An unmounted priced disk stays at its price; a shared disk and an unpriced one carry no cost over."""
        data = [
            attached(0, disk("data-one")),
            attached(1, disk("spare")),
            attached(2, disk("shared-one", shared=True)),
            attached(3, disk("unpriced")),
        ]
        prices = {"data-one": 500.0, "spare": 40.0, "shared-one": 70.0, "unpriced": None}
        result = self.assess(
            vm("vm-one", *data), guest({0: ("/data",)}, {"/data": 100}), prices, {"data-one": busy(100)}
        )
        saving = result.saving
        if saving is None:
            raise AssertionError(result.reasons)
        self.assertEqual(
            ([item.id for item in saving.staying], saving.staying_cost, saving.monthly), (["spare"], 40.0, 116.0)
        )
        self.assertEqual((result.current, result.price_note), (None, "no list price for E30 LRS in westeurope"))

    def test_a_disk_with_no_metric_data_has_no_activity(self) -> None:
        """An empty answer is 14 days with no data point."""
        self.assertEqual(activity_of({}), Activity(None, "no metric data in the last 14 days"))


class Listed:
    """An inventory that gives the same VMs at each read."""

    def __init__(self, inventory: Inventory) -> None:
        """Give inventory."""
        self._inventory = inventory

    def read(self, progress: Progress) -> Inventory:
        """Give the inventory."""
        return self._inventory


class NotRead:
    """A cloud whose VMs the run must neither read inside nor measure."""

    def question(self, vms: Sequence[Vm]) -> str:
        """Fail: no VM here may be asked about."""
        raise AssertionError(f"asked about {[item.name for item in vms]}")

    def read(self, vm: Vm) -> Guest:
        """Fail: no VM here may be read inside."""
        raise AssertionError(f"read inside {vm.name}")

    def hourly(self, disk: Disk, window: Window) -> Hourly:
        """Fail: no disk here may be measured."""
        raise AssertionError(f"measured {disk.id}")


class Unreachable:
    """A price list that never answers, with a reason that names the region."""

    def monthly(self, region: str, disk: DiskSpec) -> float:
        """Fail for region."""
        raise PriceListUnreachable(f"the price list did not answer for {region}")


class PricesTest(unittest.TestCase):
    """DisksAssessment's list prices."""

    def test_a_price_list_that_cannot_be_read_is_named_once_for_each_cloud(self) -> None:
        """Each disk says its region's price list was not read; each cloud's line gives the first reason in order."""
        windows = [
            replace(vm("vm-west", attached(0, disk("data-west"))), os_type="Windows"),
            replace(vm("vm-north", attached(0, disk("data-north", "northeurope"))), os_type="Windows"),
        ]
        volume = Disk("vol-01", AWS, "eu-west-1", "gp2", 500, "", 1500, None, False, False)
        instance = replace(vm("i-01", attached(0, volume)), cloud=AWS, os_type="Windows", limits=None)
        clouds = [
            DiskCloud(
                AZURE,
                Listed(Inventory(tuple(windows), (), (ScopeRead("one", 2, 2),), (), {})),
                NotRead(),
                NotRead(),
                Unreachable(),
            ),
            DiskCloud(
                AWS,
                Listed(Inventory((instance,), (), (ScopeRead("eu-west-1", 1, 2),), (), {})),
                NotRead(),
                NotRead(),
                Unreachable(),
            ),
        ]
        console = CapturingConsole()
        prompt = ReplayPrompt([])
        result = DisksAssessment(clouds, VirtualClock(FACTS.started), prompt, console.part("Disks")).run()
        self.assertEqual(
            console.lines["Disks"],
            [
                "Subscription one: 2 VMs, 2 disks",
                "Region eu-west-1: 1 instance, 2 volumes",
                "Disk list prices: the price list did not answer for northeurope",
                "Disk list prices: the price list did not answer for eu-west-1",
            ],
        )
        self.assertEqual(
            [(item.vm.name, item.current, item.price_note) for item in result.vms],
            [
                ("vm-west", None, "price list not readable for westeurope"),
                ("vm-north", None, "price list not readable for northeurope"),
                ("i-01", None, "price list not readable for eu-west-1"),
            ],
        )
        self.assertEqual((result.today, prompt.unused()), (0, []))
