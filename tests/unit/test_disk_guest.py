"""The read inside a VM: the command's output, the question that asks for it, and both clouds' ways to run it."""

import unittest
from typing import Sequence, Tuple

from tests.replay.disks import (
    ACCOUNT,
    AWS_KERNEL,
    AZURE_KERNEL,
    GIB,
    aws_call,
    aws_failure,
    guest_output,
    invocation,
    run_command,
    ssm_poll,
    ssm_send,
    vm_id,
)
from tests.unit.clis import CloudTestCase
from zoorik.cloud.aws import AwsCli
from zoorik.cloud.azure import AzureCli
from zoorik.cloud.keys import AWS, AZURE
from zoorik.cloud.started import StartedCommands
from zoorik.disks.guest import (
    INCOMPLETE,
    UNREADABLE,
    Printed,
    RunCommandReader,
    SsmReader,
    kernel_version,
    listing,
    minutes,
    parse,
)
from zoorik.disks.model import DataDisk, Disk, Guest, Vm
from zoorik.errors import CommandFailed, CommandTimedOut, UnexpectedResponse

NOT_DELIVERED = (
    "An error occurred (InvocationDoesNotExist) when calling the GetCommandInvocation operation: Command "
    "invocation does not exist.\n"
)


def vm(name: str, account: str, cloud: str = AZURE, data: Tuple[DataDisk, ...] = ()) -> Vm:
    """Make a running Linux VM in an account, with data disks."""
    region = "westeurope" if cloud == AZURE else "eu-west-1"
    ident = vm_id(name) if cloud == AZURE else name
    return Vm(ident, name, cloud, account, region, None, "size", "Linux", "Running", data, None, None, None, "")


def volume(lun: int, volume_id: str) -> DataDisk:
    """Make an EBS data volume at a LUN."""
    disk = Disk(volume_id, AWS, "eu-west-1", "gp2", 500, "", 1500, None, False, False)
    return DataDisk(lun, 500, disk, "", f"/dev/sd{'fgh'[lun]}")


class ParseTest(unittest.TestCase):
    """parse and kernel_version."""

    def test_the_output_gives_the_kernel_btrfs_each_disks_mount_points_and_each_mounts_used_bytes(self) -> None:
        """The escapes lsblk writes are read; a LUN that isn't a number and a used figure that isn't are passed over."""
        lines = ["S", "K 5.15.0-1057-azure", "B loaded", "L 0  /data /data/more ", "L x  /odd "]
        lines += ["V vol-01  /srv\\x20files ", "D 1073741824 /", "D 2147483648 /srv files", "D n/a /bad", "E"]
        self.assertEqual(
            parse("\r\n".join(lines) + "\r\n"),
            Printed(
                kernel="5.15.0-1057-azure",
                btrfs="loaded",
                luns={0: ("/data", "/data/more")},
                volumes={"vol-01": ("/srv files",)},
                mounts={"/": GIB, "/srv files": 2 * GIB},
            ),
        )

    def test_output_cut_short_or_without_a_kernel_or_btrfs_state_is_not_read(self) -> None:
        """No S or no E line: incomplete; no kernel, or a btrfs state the command never prints: not readable."""
        for text, reason in (
            ("K 5.15.0\nB loaded\n", INCOMPLETE),
            ("S\nB loaded\nE\n", UNREADABLE),
            ("S\nK 5.15.0\nB maybe\nE\n", UNREADABLE),
        ):
            with self.subTest(text), self.assertRaisesRegex(UnexpectedResponse, f"^{reason}$"):
                parse(text)

    def test_a_kernel_gives_its_major_and_minor_version(self) -> None:
        """5.15.0-1057-azure is 5.15; a kernel that starts with no version has none."""
        self.assertEqual((kernel_version(AZURE_KERNEL), kernel_version("unknown")), ((5, 15), None))


class QuestionTest(unittest.TestCase):
    """listing and minutes."""

    def test_the_question_names_20_vms_at_most_by_group(self) -> None:
        """Groups in name order: 15 names, 2, then 3 of 8 with the rest counted; groups past 20 names are counted."""
        vms = [vm(f"a-{index:02}", "one") for index in range(15)] + [vm(f"b-{index}", "two") for index in range(8)]
        vms += [vm("c-0", "three"), vm("c-1", "three")]
        lines = listing(vms, "subscription", lambda item: item.account)
        self.assertEqual(lines[0], "    one: " + ", ".join(f"a-{index:02}" for index in range(15)))
        self.assertEqual(lines[1:], ["    three: c-0, c-1", "    two: b-0, b-1, b-2 and 5 more"])
        many = [vm(f"vm-{index:02}", f"group-{index:02}") for index in range(22)]
        self.assertEqual(listing(many, "region", lambda item: item.account)[-1], "    and 2 more in 2 other regions")

    def test_the_estimate_is_45_seconds_for_each_8_vms(self) -> None:
        """Up to 8 VMs take about a minute; 100 take 13 rounds of 45 s, about 10 minutes."""
        self.assertEqual((minutes(8), minutes(100)), (1, 10))


class RunCommandReaderTest(CloudTestCase):
    """RunCommandReader."""

    def test_the_command_runs_through_run_command_and_each_data_disk_is_found_by_its_lun(self) -> None:
        """What the command printed is read; the start is kept, so a stop can name it."""
        printed = guest_output("L", [("0", ["/data"])], {"/data": 100}, AZURE_KERNEL)
        host, _ = self.host([run_command("vm-one", printed)])
        started = StartedCommands()
        reader = RunCommandReader(AzureCli(host, started), {"sub-one": "subscription-one"})
        guest = reader.read(vm("vm-one", "sub-one"))
        self.assertEqual(guest, Guest(AZURE_KERNEL, "module", {0: ("/data",)}, {"/": 8 * GIB, "/data": 100 * GIB}))
        self.assertIn("Azure Run Command had already started in 1 VM", started.note())

    def test_the_question_lists_the_vms_by_subscription_and_shows_the_command(self) -> None:
        """Each VM is listed under its subscription's name, and the question ends asking y/N."""
        reader = RunCommandReader(AzureCli(self.host([])[0], StartedCommands()), {"sub-one": "one", "sub-two": "two"})
        question = reader.question([vm("vm-b", "sub-two"), vm("vm-a", "sub-one")])
        self.assertIn("  2 running Linux VMs have data disks:\n    one: vm-a\n    two: vm-b\n", question)
        self.assertIn("    echo S\n", question)
        self.assertTrue(question.endswith("Read disk usage inside 2 VMs with Azure Run Command? [y/N] "))
        one = reader.question([vm("vm-a", "sub-one")])
        self.assertIn("  1 running Linux VM has data disks:\n    one: vm-a\n", one)
        self.assertTrue(one.endswith("Read disk usage inside 1 VM with Azure Run Command? [y/N] "))


class SsmReaderTest(CloudTestCase):
    """SsmReader."""

    def reader(self, answers: Sequence[object]) -> Tuple[SsmReader, Vm]:
        """Make a reader whose command to i-01 gets answers, each a status or a failure's error output."""
        polls = [
            aws_failure(ssm_poll("i-01"), answer) if isinstance(answer, str) else aws_call(ssm_poll("i-01"), answer)
            for answer in answers
        ]
        host, _ = self.host([ssm_send("i-01"), *polls])
        instance = vm("i-01", ACCOUNT, AWS, (volume(0, "vol-01"), volume(1, "vol-02")))
        return SsmReader(AwsCli(host, StartedCommands()), self.clock), instance

    def test_the_question_counts_instances(self) -> None:
        """The question counts instances, one as one and more as many, and ends asking y/N."""
        reader, instance = self.reader([])
        self.assertIn("  1 running Linux instance has data volumes:\n", reader.question([instance]))
        second = vm("i-02", ACCOUNT, AWS)
        question = reader.question([instance, second])
        self.assertIn("  2 running Linux instances have data volumes:\n", question)
        self.assertTrue(
            question.endswith("Read disk usage inside 2 instances with Systems Manager Run Command? [y/N] ")
        )

    def test_each_volume_is_matched_to_its_data_disk_by_its_id(self) -> None:
        """A command not yet delivered is asked about again 3 s later; a volume not printed has no mount points."""
        printed = guest_output("V", [("vol-01", ["/data"])], {"/data": 100}, AWS_KERNEL)
        reader, instance = self.reader([NOT_DELIVERED, invocation("Success", printed)])
        self.assertEqual(reader.read(instance).luns, {0: ("/data",)})
        self.assertEqual(self.clock.monotonic(), 6.0)

    def test_a_command_that_ends_otherwise_names_how_it_ended(self) -> None:
        """A failed command is named by its status details."""
        reader, instance = self.reader([invocation("Failed")])
        with self.assertRaisesRegex(CommandFailed, "^the command ended as Failed on the instance$"):
            reader.read(instance)

    def test_a_command_still_running_after_its_delivery_time_has_no_answer_in_time(self) -> None:
        """Systems Manager may take 300 s to deliver it: 100 polls 3 s apart."""
        reader, instance = self.reader([invocation("InProgress")] * 100)
        with self.assertRaisesRegex(CommandTimedOut, "^no answer in time$"):
            reader.read(instance)
