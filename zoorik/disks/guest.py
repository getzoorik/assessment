"""The read inside a VM, made only after a yes: one command that only reads, and what it printed.

The command goes through Azure Run Command or AWS Systems Manager Run Command. The two clouds differ only in how a data
disk shows in the VM: by LUN on Azure, by volume id (an NVMe serial) on AWS.
"""

import math
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from zoorik.cloud.aws import SSM_DELIVERY_SECONDS, SSM_DOCUMENT, AwsCli
from zoorik.cloud.azure import AzureCli
from zoorik.cloud.keys import AWS, AZURE
from zoorik.disks.model import Guest, Vm
from zoorik.errors import CommandFailed, CommandTimedOut, UnexpectedResponse
from zoorik.format import SECONDS_PER_MINUTE, count
from zoorik.system.clock import Clock
from zoorik.system.workers import WORKERS

GUEST_HEAD = (
    r"""echo S
echo "K $(uname -r)"
"""
    r"""if grep -qw btrfs /proc/filesystems; then echo "B loaded"; """
    r"""elif modinfo -n btrfs >/dev/null 2>&1; then echo "B module"; else echo "B absent"; fi
"""
)
GUEST_TAIL = (
    r"""
timeout 30 df -P -B1 -l | awk '$1 ~ /^\/dev\// && $1 !~ /^\/dev\/loop/ """
    r"""{m=$6; for (i=7; i<=NF; i++) m=m" "$i; print "D", $3, m}'
echo E"""
)
AZURE_DISKS = (
    r"""for l in /dev/disk/azure/scsi1/lun*; do n=${l##*/lun}; case $n in ''|*[!0-9]*) continue;; esac; """
    r"""echo "L $n $(lsblk -nro MOUNTPOINT "$l" | tr '\n' ' ')"; done"""
)
AWS_DISKS = (
    r"""for l in /dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol*; do case $l in *-part*) continue;; esac; """
    r"""[ -e "$l" ] || continue; v=${l##*_vol}; """
    r"""echo "V vol-${v%%[-_]*} $(lsblk -nro MOUNTPOINT "$l" | tr '\n' ' ')"; done"""
)
GUEST_SCRIPT = {AZURE: GUEST_HEAD + AZURE_DISKS + GUEST_TAIL, AWS: GUEST_HEAD + AWS_DISKS + GUEST_TAIL}

START = "S"
END = "E"
BTRFS_STATES = ("loaded", "module", "absent")
INCOMPLETE = "the command's output came back incomplete"
UNREADABLE = "the command's output was not readable"
LSBLK_ESCAPE = re.compile(r"\\x([0-9a-fA-F]{2})")
HEX = 16

# How long one read takes, for the question's estimate.
RUN_COMMAND_SECONDS = 45
RUN_COMMAND_PERMISSION = "Microsoft.Compute/virtualMachines/runCommand/action"
SSM_PERMISSIONS = "ssm:SendCommand and ssm:GetCommandInvocation"
SSM_POLL_SECONDS = 3
SSM_WAITING = ("Pending", "InProgress", "Delayed")
SSM_SUCCESS = "Success"
# The question names this many VMs at most, and counts the rest.
LISTED_NAMES = 20


@dataclass(frozen=True)
class Printed:
    """What the command printed: the kernel, btrfs, the mount points on each data disk, and each mount's used bytes."""

    kernel: str
    btrfs: str
    # The mount points on each data disk: by LUN on Azure, by volume id on AWS.
    luns: Mapping[int, Tuple[str, ...]]
    volumes: Mapping[str, Tuple[str, ...]]
    mounts: Mapping[str, int]


class GuestReader(Protocol):
    """A cloud's way to read inside its VMs."""

    def question(self, vms: Sequence[Vm]) -> str:
        """Give the y/N question that asks to read inside vms."""

    def read(self, vm: Vm) -> Guest:
        """Run the command in vm and give what it found."""


class RunCommandReader:
    """Reads inside Azure VMs through Run Command."""

    def __init__(self, cli: AzureCli, subscriptions: Mapping[str, str]) -> None:
        """Run the command through cli; the question names each VM's subscription from subscriptions, by id."""
        self._cli = cli
        self._subscriptions = subscriptions

    def question(self, vms: Sequence[Vm]) -> str:
        """Give the y/N question that asks to read inside vms, listed by subscription."""
        number = len(vms)
        listed = listing(vms, "subscription", self._group)
        return (
            f"\nDisk usage\n"
            f"  {count(number, 'running Linux VM')} {have(number)} data disks:\n" + "\n".join(listed) + "\n\n"
            "  The saving needs the used space on those disks, which only the VM itself can report.\n"
            "  With your yes, this command, which only reads, runs in each VM through Azure Run\n"
            f"  Command, {WORKERS} VMs at a time (about {minutes(number)} min):\n\n"
            f"{indented(GUEST_SCRIPT[AZURE])}\n\n"
            "  On its side, Azure adds its Run Command extension (RunCommandLinux) to a VM that does not\n"
            "  have it yet, keeps a copy of the command and its output under /var/lib/waagent on the VM,\n"
            "  and records each run in the activity log. It needs the permission\n"
            f"  {RUN_COMMAND_PERMISSION} (Virtual Machine Contributor has it);\n"
            "  a VM without it shows no saving, with the reason.\n\n"
            f"Read disk usage inside {count(number, 'VM')} with Azure Run Command? [y/N] "
        )

    def read(self, vm: Vm) -> Guest:
        """Run the command in the VM and give what it found, each data disk by its LUN."""
        printed = parse(self._cli.run_shell_script(vm.id, GUEST_SCRIPT[AZURE]))
        return Guest(printed.kernel, printed.btrfs, printed.luns, printed.mounts)

    def _group(self, vm: Vm) -> str:
        """Name the subscription the question lists the VM under."""
        return self._subscriptions[vm.account]


class SsmReader:
    """Reads inside EC2 instances through Systems Manager Run Command."""

    def __init__(self, cli: AwsCli, clock: Clock) -> None:
        """Send the command through cli, waiting for its answer on clock."""
        self._cli = cli
        self._clock = clock

    def question(self, vms: Sequence[Vm]) -> str:
        """Give the y/N question that asks to read inside vms, listed by region."""
        number = len(vms)
        listed = listing(vms, "region", region_of)
        return (
            f"\nDisk usage\n"
            f"  {count(number, 'running Linux instance')} {have(number)} data volumes:\n" + "\n".join(listed) + "\n\n"
            "  The saving needs the used space on those volumes, which only the instance itself can report.\n"
            "  With your yes, this command, which only reads, runs in each instance through AWS Systems\n"
            f"  Manager Run Command ({SSM_DOCUMENT}), {WORKERS} instances at a time (about {minutes(number)} min):\n\n"
            f"{indented(GUEST_SCRIPT[AWS])}\n\n"
            "  On its side, Systems Manager keeps the command and its output in its Run Command history,\n"
            "  its agent keeps a copy under /var/lib/amazon/ssm on the instance, and CloudTrail records\n"
            f"  each call. It needs the permissions {SSM_PERMISSIONS};\n"
            "  an instance without them shows no saving, with the reason.\n\n"
            f"Read disk usage inside {count(number, 'instance')} with Systems Manager Run Command? [y/N] "
        )

    def read(self, vm: Vm) -> Guest:
        """Send the command to the instance, wait for it, and give what it found, each volume matched by its id."""
        command = self._cli.send_command(vm.region, vm.id, GUEST_SCRIPT[AWS])
        printed = parse(self._answer(vm, command))
        luns = {
            disk.lun: printed.volumes[disk.disk.id]
            for disk in vm.data
            if disk.disk is not None and disk.disk.id in printed.volumes
        }
        return Guest(printed.kernel, printed.btrfs, luns, printed.mounts)

    def _answer(self, vm: Vm, command: str) -> str:
        """Wait for the command to end on the instance, as long as Systems Manager may take, and give its output."""
        deadline = self._clock.monotonic() + SSM_DELIVERY_SECONDS
        while self._clock.monotonic() < deadline:
            self._clock.sleep(SSM_POLL_SECONDS)
            invocation = self._cli.command_invocation(vm.region, command, vm.id)
            # The command has not reached the instance yet.
            if invocation is None:
                continue
            if invocation.status == SSM_SUCCESS:
                return invocation.stdout
            if invocation.status not in SSM_WAITING:
                raise CommandFailed(f"the command ended as {invocation.details} on the instance")
        raise CommandTimedOut("no answer in time")


def region_of(vm: Vm) -> str:
    """Name the region the question lists the instance under."""
    return vm.region


def minutes(number: int) -> int:
    """Estimate how many minutes reading inside number VMs takes, WORKERS at a time."""
    return max(1, math.ceil(math.ceil(number / WORKERS) * RUN_COMMAND_SECONDS / SECONDS_PER_MINUTE))


def have(number: int) -> str:
    """Give the verb the question's count of VMs takes: has for one, have for more."""
    return "has" if number == 1 else "have"


def indented(script: str) -> str:
    """Give the command as the question shows it, each line indented."""
    return "\n".join("    " + line for line in script.splitlines())


def listing(vms: Sequence[Vm], kind: str, group_of: Callable[[Vm], str]) -> List[str]:
    """List the VMs' names under each group, LISTED_NAMES at most, then how many more there are."""
    names: Dict[str, List[str]] = {}
    for vm in vms:
        names.setdefault(group_of(vm), []).append(vm.name)
    lines: List[str] = []
    left, rest, rest_groups = LISTED_NAMES, 0, 0
    for group in sorted(names):
        in_group = sorted(names[group])
        if left <= 0:
            rest, rest_groups = rest + len(in_group), rest_groups + 1
            continue
        shown = in_group[:left]
        left -= len(shown)
        more = len(in_group) - len(shown)
        lines.append(f"    {group}: {', '.join(shown)}" + (f" and {more} more" if more else ""))
    if rest:
        lines.append(f"    and {rest} more in {count(rest_groups, f'other {kind}')}")
    return lines


def parse(text: str) -> Printed:
    """Read what the command printed between its S and E lines."""
    lines = [line.strip("\r") for line in text.splitlines()]
    if START not in lines or END not in lines:
        raise UnexpectedResponse(INCOMPLETE)
    kernel = btrfs = ""
    luns: Dict[int, Tuple[str, ...]] = {}
    volumes: Dict[str, Tuple[str, ...]] = {}
    mounts: Dict[str, int] = {}
    for line in lines:
        tag, _, value = line.partition(" ")
        if tag == "K":
            kernel = value.strip()
        elif tag == "B":
            btrfs = value.strip()
        elif tag == "L":
            add_lun(luns, value)
        elif tag == "V":
            volume, points = mount_points(value)
            volumes[volume] = points
        elif tag == "D":
            add_mount(mounts, value)
    if not kernel or btrfs not in BTRFS_STATES:
        raise UnexpectedResponse(UNREADABLE)
    return Printed(kernel, btrfs, luns, volumes, mounts)


def mount_points(value: str) -> Tuple[str, Tuple[str, ...]]:
    """Read an L or V line: the disk, and the mount points on it."""
    disk, _, points = value.partition(" ")
    return disk, tuple(unescape_lsblk(point) for point in points.split() if point.startswith("/"))


def add_lun(luns: Dict[int, Tuple[str, ...]], value: str) -> None:
    """Add an L line's LUN and its mount points; a LUN that isn't a number is not a data disk's."""
    lun, points = mount_points(value)
    if lun.isdigit():
        luns[int(lun)] = points


def add_mount(mounts: Dict[str, int], value: str) -> None:
    """Add a D line's mount and its used bytes, when both are there."""
    used, _, mount = value.partition(" ")
    if used.isdigit() and mount:
        mounts[mount] = int(used)


def unescape_lsblk(text: str) -> str:
    r"""Write lsblk's \xHH escapes, such as \x20 for a space in a mount point, as their characters."""
    return LSBLK_ESCAPE.sub(lambda match: chr(int(match.group(1), HEX)), text)


def kernel_version(kernel: str) -> Optional[Tuple[int, int]]:
    """Give a Linux kernel's major and minor version, such as (5, 15) for 5.15.0-1057-azure; None when it has none."""
    match = re.match(r"(\d+)\.(\d+)", kernel)
    return None if match is None else (int(match.group(1)), int(match.group(2)))
