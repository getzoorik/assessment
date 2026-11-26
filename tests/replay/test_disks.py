"""The disk part on made-up estates: each scenario's result, lines, questions and started commands, against goldens."""

import dataclasses
import tempfile
import unittest
from typing import Dict, List, Optional, Sequence

from tests.replay import golden
from tests.replay.disks import SCENARIOS, DiskScenario
from tests.replay.fake_cloud import FakeRun
from tests.replay.replay import ReplayHttp, ReplayPrompt, ReplayRunner
from tests.replay.scenarios import FACTS
from zoorik.cloud.aws import AwsCli
from zoorik.cloud.azure import AzureCli
from zoorik.cloud.cli import Host
from zoorik.cloud.started import StartedCommands
from zoorik.disks.assessment import SHARED, DiskCloud, DisksAssessment, aws_disks, azure_disks
from zoorik.disks.model import DisksResult, Saving, VmResult
from zoorik.pricing.aws_ebs import AwsEbsPrices
from zoorik.pricing.azure_disks import AzureDiskPrices

PART = "Disks"
BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}


class AskedPrompt:
    """A prompt that gives the made-up answers in order, and keeps each question it is asked."""

    def __init__(self, answers: Sequence[Optional[bool]]) -> None:
        """Answer with answers, in order."""
        self._replay = ReplayPrompt(answers)
        self.questions: List[str] = []

    def ask(self, question: str) -> Optional[bool]:
        """Keep the question, and give the next answer."""
        self.questions.append(question)
        return self._replay.ask(question)

    def unused(self) -> List[str]:
        """Name each answer no question took."""
        return self._replay.unused()


@dataclasses.dataclass(frozen=True)
class Outcome:
    """A scenario's run: its result, the document its golden holds, and the answers it never used."""

    result: DisksResult
    document: Dict[str, object]
    unused: List[str]


def run(scenario: DiskScenario) -> Outcome:
    """Run the disk part on the scenario's answers."""
    with tempfile.TemporaryDirectory() as temp_root:
        fake = FakeRun(FACTS, temp_root)
        runner = ReplayRunner(scenario.calls, temp_root, fake.clock)
        http = ReplayHttp(scenario.fetches)
        prompt = AskedPrompt(scenario.answers)
        started = StartedCommands()
        host = Host(runner, fake.clock, temp_root, False)
        clouds: List[DiskCloud] = []
        if scenario.azure is not None:
            clouds.append(azure_disks(AzureCli(host, started), scenario.azure, AzureDiskPrices(http)))
        if scenario.aws is not None:
            aws = AwsCli(host, started)
            clouds.append(aws_disks(aws, scenario.aws, AwsEbsPrices(aws), fake.clock))
        result = DisksAssessment(clouds, fake.clock, prompt, fake.console.part(PART)).run()
    document: Dict[str, object] = {
        "console": fake.console.lines.get(PART, []),
        "questions": prompt.questions,
        "started": started.note(),
        "result": {**dataclasses.asdict(result), "saving": result.saving, "after": result.after},
    }
    return Outcome(result, document, [*runner.unused(), *http.unused(), *prompt.unused()])


def result_of(name: str) -> DisksResult:
    """Give the result of the scenario called name."""
    return run(BY_NAME[name]).result


def vm_named(result: DisksResult, name: str) -> VmResult:
    """Give the result of the VM called name."""
    return next(vm for vm in result.vms if vm.vm.name == name)


def saving_of(result: DisksResult, name: str) -> Saving:
    """Give the saving of the VM called name, which must have one."""
    saving = vm_named(result, name).saving
    if saving is None:
        raise AssertionError(f"{name} has no saving: {vm_named(result, name).reasons}")
    return saving


class DisksGoldenTest(unittest.TestCase):
    """Each scenario's run matches its golden, and takes every answer it was given."""

    def test_each_scenario_matches_its_golden(self) -> None:
        """The result, the lines, the questions and the started commands are each scenario's golden."""
        for scenario in SCENARIOS:
            with self.subTest(scenario.name):
                outcome = run(scenario)
                self.assertEqual(outcome.unused, [])
                golden.check(f"disks_{scenario.name.replace('-', '_')}", outcome.document)


class DisksFiguresTest(unittest.TestCase):
    """The figures each scenario's docstring works out by hand."""

    def test_a_linux_vm_in_a_zone_saves_on_premium_ssd_v2(self) -> None:
        """328 GB on Premium SSD v2 at 23.93, against 200 of disks: 176.07; page 1 rounds each VM's figures first."""
        result = result_of("linux-vm")
        saving = saving_of(result, "vm-one")
        self.assertEqual((saving.pool_gb, saving.pool_cost, saving.today, saving.monthly), (328, 23.93, 200.0, 176.07))
        self.assertEqual((result.today, result.saving, result.after), (200, 176, 24))

    def test_the_cost_today_counts_a_shared_disk_once_and_a_half_priced_vms_priced_disk(self) -> None:
        """The shared P20 counts once beside the P30 (70 + 130); a VM with an unpriced disk adds its priced one."""
        shared = result_of("shared-disk")
        self.assertEqual(shared.today, 200)
        self.assertEqual(vm_named(shared, "vm-two").reasons, (SHARED,))
        half = result_of("priced-and-unpriced")
        self.assertEqual(half.today, 130)
        self.assertIsNone(vm_named(half, "vm-one").current)

    def test_a_vm_in_no_zone_saves_on_premium_ssd(self) -> None:
        """Three P10s at 20 against a P30 at 130: 70, beside the shared disk, which stays."""
        saving = saving_of(result_of("shared-disk"), "vm-one")
        self.assertEqual((saving.pool_gb, saving.pool_cost, saving.today, saving.monthly), (384, 60.0, 130.0, 70.0))
        self.assertEqual(saving.staying, ())

    def test_a_disk_with_no_record_is_left_out_of_the_figures(self) -> None:
        """The pool replaces the P30 alone: 200 GB at 14.59, a saving of 115.41; the cost today is the P30's."""
        result = result_of("missing-disk-record")
        saving = saving_of(result, "vm-one")
        self.assertEqual((saving.pool_gb, saving.pool_cost, saving.monthly), (200, 14.59, 115.41))
        self.assertEqual((result.today, vm_named(result, "vm-one").price_note), (130, "disk details not readable"))

    def test_an_hour_with_no_maximum_is_left_out(self) -> None:
        """The pool is sized on the other hour alone: three P10s at 60 against the P30's 130."""
        saving = saving_of(result_of("hour-without-maximum"), "vm-one")
        self.assertEqual((saving.pool_gb, saving.pool_cost, saving.monthly), (384, 60.0, 70.0))

    def test_an_instance_saves_on_gp3(self) -> None:
        """Two 58 GiB gp3 volumes at 9.28 against a 500 GiB gp2 at 50: 40.72."""
        saving = saving_of(result_of("self-managed-node"), "i-01")
        self.assertEqual((saving.pool_gb, saving.pool_cost, saving.today, saving.monthly), (116, 9.28, 50.0, 40.72))

    def test_gp2_gp3_and_io1_volumes_go_on_one_gp3_pool(self) -> None:
        """Two 252 GiB gp3 volumes at 156 MiB/s, 42.80, against 420 of volumes: 377.20."""
        saving = saving_of(result_of("ebs-types"), "i-01")
        self.assertEqual((saving.pool_gb, saving.pool_cost, saving.today, saving.monthly), (504, 42.8, 420.0, 377.2))
