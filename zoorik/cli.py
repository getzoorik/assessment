"""The zoorik command: zoorik assess reads every signed-in cloud and writes one report.

run() is the whole command on a Runtime, which tests replace, and main() runs it on this computer. It reads what each
cloud's CLI covers, runs the Kubernetes and Disks parts side by side, writes the report and says what it found. A stop
signal ends the run at once, with a note of what had started in the user's cloud. Nothing imports this module.
"""

import argparse
import os
import shutil
import sys
import traceback
from concurrent import futures
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, NoReturn, Optional, Sequence, Set, Tuple, TypeVar, Union

from zoorik import CONTACT, __version__
from zoorik.cloud.aws import AwsCli, AwsScope
from zoorik.cloud.azure import AzureCli, AzureScope, OtherDirectory
from zoorik.cloud.cli import Host
from zoorik.cloud.shell import CloudShell, cloud_shell
from zoorik.cloud.started import StartedCommands
from zoorik.console import Console
from zoorik.disks.assessment import SCOPE_WORDS, DiskCloud, DisksAssessment, aws_disks, azure_disks
from zoorik.disks.model import DisksResult
from zoorik.errors import AssessmentError, ReportNotWritable, ToolNotInstalled
from zoorik.format import count, usd
from zoorik.kubectl.direct import DirectKubeApi
from zoorik.kubectl.invoke import InvokeKubeApi
from zoorik.kubectl.usage import DEFAULT_SAMPLE_MINUTES, SampleWindow
from zoorik.kubernetes.assessment import ClusterCloud, KubernetesAssessment, aws_clusters, azure_clusters
from zoorik.kubernetes.discovery import Readers
from zoorik.kubernetes.model import ClusterResult, KubernetesResult, LineResult
from zoorik.output import report_folder, write_report
from zoorik.pricing.aws_ebs import AwsEbsPrices
from zoorik.pricing.azure_disks import AzureDiskPrices
from zoorik.pricing.http import UrllibHttpClient
from zoorik.pricing.machines import AwsMachinePrices, AzureMachinePrices
from zoorik.report.closing import CloudUnread
from zoorik.report.document import Run, report
from zoorik.report.names import Names
from zoorik.report.summary import Summary, summary
from zoorik.system.clock import SystemClock
from zoorik.system.runtime import Runtime, real_runtime

KUBERNETES = "Kubernetes"
DISKS = "Disks"
# A run a signal stops exits with 128 plus the signal's number, as a shell reports it: Ctrl-C gives 130.
SIGNAL_EXIT = 128
# How often the main thread wakes while it waits, so a stop signal's handler runs at once on every system.
WAIT_SECONDS = 0.25
PACKAGE = os.path.dirname(os.path.abspath(__file__))
DESCRIPTION = (
    "Zoorik's free cloud cost assessment. It reads the AKS and EKS clusters and the VM data disks of every Azure and "
    f"AWS account the az and aws CLIs are signed in to, and writes one PDF report for you to email to {CONTACT}."
)
ASSESS = "read every signed-in Azure and AWS account and write the report"
START = "Zoorik cloud cost assessment {version}. It sends nothing to Zoorik; you send the report yourself."
IDLE = (
    "Cloud Shell closes a session after 20 minutes without a key press, so press Enter now and then until the "
    "report's path appears. If the run asks about disks first, answer it y or N: Enter alone answers N."
)
NO_CLI = (
    "This needs the Azure CLI (az) or the AWS CLI (aws), signed in. Open Azure Cloud Shell (shell.azure.com) or AWS "
    "CloudShell and run it there."
)
NOTHING = "Nothing to report: the run found no cluster machine or VM data disk with a list price."
STOPPED = "Stopped."
DOWNLOAD: Dict[Optional[CloudShell], Tuple[str, ...]] = {
    None: (),
    CloudShell.AZURE: ("Download it: Cloud Shell toolbar > Manage files > Download, then enter {path}",),
    CloudShell.AWS: (
        "Download it: Actions > Download file, then enter {path}",
        "(Download needs the full CloudShell page; if Actions is missing, open CloudShell in a new tab.)",
    ),
}

Scope = TypeVar("Scope")
First = TypeVar("First")
Second = TypeVar("Second")


@dataclass(frozen=True)
class Options:
    """What zoorik assess was asked for."""

    out: Optional[str]
    sample_minutes: int
    no_invoke: bool
    anonymize: bool


@dataclass(frozen=True)
class Clouds:
    """The two cloud CLIs, and what each one covers or why it couldn't be read, such as not being installed."""

    azure: AzureCli
    aws: AwsCli
    azure_scope: Union[AzureScope, AssessmentError]
    aws_scope: Union[AwsScope, AssessmentError]

    @property
    def installed(self) -> bool:
        """Tell whether either CLI is installed."""
        return not all(isinstance(scope, ToolNotInstalled) for scope in (self.azure_scope, self.aws_scope))

    @property
    def read(self) -> Tuple[str, ...]:
        """Name the clouds whose CLI read what it covers."""
        found = ((self.azure.cloud, self.azure_scope), (self.aws.cloud, self.aws_scope))
        return tuple(cloud for cloud, scope in found if not isinstance(scope, AssessmentError))

    @property
    def unread(self) -> Tuple[CloudUnread, ...]:
        """Name each cloud whose CLI is installed but couldn't read, with why."""
        found = ((self.azure.cloud, self.azure_scope), (self.aws.cloud, self.aws_scope))
        return tuple(
            CloudUnread(cloud, str(scope))
            for cloud, scope in found
            if isinstance(scope, AssessmentError) and not isinstance(scope, ToolNotInstalled)
        )

    @property
    def elsewhere(self) -> Tuple[OtherDirectory, ...]:
        """Give the Azure directories the signed-in account reaches but the run doesn't read."""
        return self.azure_scope.elsewhere if isinstance(self.azure_scope, AzureScope) else ()

    def lines(self) -> List[str]:
        """Say what each installed CLI covers, or why it couldn't be read."""
        lines: List[str] = []
        if isinstance(self.azure_scope, AzureScope):
            subscriptions = count(len(self.azure_scope.subscriptions), "enabled subscription")
            lines.append(f"{self.azure.cloud}: {self.azure_scope.label}, {subscriptions}")
            lines += [f"{self.azure.cloud}: {other.note}" for other in self.azure_scope.elsewhere]
        if isinstance(self.aws_scope, AwsScope):
            regions = count(len(self.aws_scope.regions), "enabled region")
            lines.append(f"{self.aws.cloud}: account {self.aws_scope.account}, {regions}")
        return lines + [f"{cloud.cloud}: {cloud.reason}" for cloud in self.unread]


def main() -> None:
    """Run the zoorik command on this computer, and exit with its code."""
    sys.exit(run(sys.argv[1:], real_runtime(UrllibHttpClient(SystemClock()))))


def run(argv: Sequence[str], runtime: Runtime) -> int:
    """Run the zoorik command with argv on runtime, and give its exit code."""
    try:
        chosen = options(argv)
    except SystemExit:
        # --help, --version and a usage error end the run here, and its temporary folder goes with it.
        remove_temp(runtime.temp_root)
        raise
    started = StartedCommands()
    runtime.install_stop(lambda signum: end(runtime, started, STOPPED, SIGNAL_EXIT + signum))
    try:
        code = assess(chosen, runtime, started)
    # The one catch-all: anything else is a defect, which ends the run with a line to send to Zoorik.
    except Exception as error:
        end(runtime, started, f"Stopped by an unexpected error ({location(error)}).", 1, contact=True)
    unremoved = remove_temp(runtime.temp_root)
    if unremoved is not None:
        runtime.console.line(unremoved)
    return code


def options(argv: Sequence[str]) -> Options:
    """Read the command line; argparse exits on --help, --version and a usage error."""
    command = argparse.ArgumentParser(prog="zoorik", description=DESCRIPTION, allow_abbrev=False)
    command.add_argument("--version", action="version", version=f"zoorik {__version__}")
    commands = command.add_subparsers(dest="command", required=True, metavar="assess", help=ASSESS)
    assess_command = commands.add_parser("assess", description=DESCRIPTION, allow_abbrev=False)
    assess_command.add_argument(
        "--out", metavar="PATH", help="the folder for the report, made if missing (default: the current folder)"
    )
    assess_command.add_argument(
        "--sample-minutes",
        metavar="N",
        type=minutes,
        default=DEFAULT_SAMPLE_MINUTES,
        help=f"how long to sample the clusters' usage (default {DEFAULT_SAMPLE_MINUTES}; 0 takes one sample)",
    )
    assess_command.add_argument(
        "--no-invoke",
        action="store_true",
        help="name the AKS clusters that only az aks command invoke can read as not assessed, instead of reading them",
    )
    assess_command.add_argument("--anonymize", action="store_true", help="hide every name the report shows")
    args = command.parse_args(argv)
    return Options(args.out, args.sample_minutes, args.no_invoke, args.anonymize)


def minutes(text: str) -> int:
    """Read --sample-minutes: a whole number of minutes, 0 or more."""
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("the sample window can't be negative")
    return value


def assess(chosen: Options, runtime: Runtime, started: StartedCommands) -> int:
    """Read every signed-in cloud, run both parts and write the report; give the exit code."""
    console = runtime.console
    try:
        folder = report_folder(chosen.out)
    except ReportNotWritable as error:
        console.line(f"Cannot write the report: {error}")
        return 1
    begun = runtime.clock.now()
    host = Host(runtime.runner, runtime.clock, runtime.temp_root, runtime.windows)
    clouds = read_clouds(host, started)
    if not clouds.installed:
        console.line(NO_CLI)
        return 0
    if not clouds.read:
        for line in clouds.lines():
            console.line(line)
        return 1
    shell = cloud_shell(runtime.env)
    for line in start_lines(clouds, shell):
        console.line(line)
    kubernetes, disks = run_parts(clouds, chosen, runtime, host)
    console.clear_status()
    figures = summary(kubernetes, disks)
    if figures.today == 0:
        return nothing_found(console, clouds, kubernetes, disks)
    facts = Run(begun, chosen.sample_minutes, clouds.read, clouds.unread, clouds.elsewhere)
    data = report(kubernetes, disks, facts, Names(runtime.salt, anonymize=chosen.anonymize))
    try:
        path = write_report(folder, data, runtime.clock.now())
    except ReportNotWritable as error:
        console.line(f"Cannot write the report: {error}")
        return 1
    for line in end_lines(figures, path, shell):
        console.line(line)
    return 0


def read_clouds(host: Host, started: StartedCommands) -> Clouds:
    """Read what both cloud CLIs cover, side by side."""
    azure, aws = AzureCli(host, started), AwsCli(host, started)
    azure_scope, aws_scope = side_by_side(lambda: attempt(azure.scope), lambda: attempt(aws.scope))
    return Clouds(azure, aws, azure_scope, aws_scope)


def attempt(read: Callable[[], Scope]) -> Union[Scope, AssessmentError]:
    """Read a cloud's scope; a typed failure, such as a CLI that isn't installed, is the result instead."""
    try:
        return read()
    except AssessmentError as error:
        return error


def start_lines(clouds: Clouds, shell: Optional[CloudShell]) -> List[str]:
    """Give the lines that open a run: what it is, in Cloud Shell the idle warning, and what each CLI covers."""
    idle = [] if shell is None else [IDLE]
    return [START.format(version=__version__), *idle, *clouds.lines()]


def run_parts(clouds: Clouds, chosen: Options, runtime: Runtime, host: Host) -> Tuple[KubernetesResult, DisksResult]:
    """Run the Kubernetes and Disks parts side by side on the clouds read, and give their results."""
    readers = Readers(lambda config: DirectKubeApi(host, config), lambda cluster: InvokeKubeApi(clouds.azure, cluster))
    cluster_clouds: List[ClusterCloud] = []
    disk_clouds: List[DiskCloud] = []
    if isinstance(clouds.azure_scope, AzureScope):
        directory = clouds.azure_scope
        cluster_clouds.append(
            azure_clusters(clouds.azure, directory, AzureMachinePrices(runtime.http), readers, chosen.no_invoke)
        )
        disk_clouds.append(azure_disks(clouds.azure, directory, AzureDiskPrices(runtime.http)))
    if isinstance(clouds.aws_scope, AwsScope):
        account = clouds.aws_scope
        cluster_clouds.append(aws_clusters(clouds.aws, account, AwsMachinePrices(clouds.aws, runtime.clock), readers))
        disk_clouds.append(aws_disks(clouds.aws, account, AwsEbsPrices(clouds.aws), runtime.clock))
    console = runtime.console
    window = SampleWindow(chosen.sample_minutes)
    kubernetes = KubernetesAssessment(cluster_clouds, window, console.part(KUBERNETES))
    disks = DisksAssessment(disk_clouds, runtime.clock, runtime.prompt, console.part(DISKS))
    return side_by_side(kubernetes.run, disks.run)


def side_by_side(first: Callable[[], First], second: Callable[[], Second]) -> Tuple[First, Second]:
    """Run first and second in two threads and give their results; a failure in either is raised at once.

    The main thread only waits, waking every WAIT_SECONDS. A stop signal's handler runs in it, so it must hold no lock
    the handler takes, and the short waits let the handler run at once on Windows too.
    """
    executor = ThreadPoolExecutor(2)
    try:
        one, two = executor.submit(first), executor.submit(second)
        waiting: Set[Future[Any]] = {one, two}
        while waiting:
            done, waiting = futures.wait(waiting, WAIT_SECONDS, futures.FIRST_EXCEPTION)
            for future in done:
                # A thread's failure is raised as soon as it is done, whether or not the other one is.
                future.result()
        return one.result(), two.result()
    finally:
        # A thread still running when the other fails is not waited for: the run is ending.
        executor.shutdown(wait=False)


def nothing_found(console: Console, clouds: Clouds, kubernetes: KubernetesResult, disks: DisksResult) -> int:
    """Say why there is nothing to report; give 1 when something couldn't be read or priced, else 0."""
    for cluster in kubernetes.clusters:
        notes = "; ".join(sorted({line.note for line in unpriced(cluster)}))
        where = cluster.cluster
        console.line(f"Cluster {where.name} ({where.region}): {f'not priced, {notes}' if notes else 'no machines'}")
    vms: Dict[Tuple[str, str], int] = {}
    for result in disks.vms:
        if result.current is None:
            key = (result.vm.cloud, result.price_note)
            vms[key] = vms.get(key, 0) + 1
    for (cloud, note), number in vms.items():
        console.line(f"{count(number, SCOPE_WORDS[cloud][1])} not priced, {note}")
    console.line(NOTHING)
    return 1 if something_left_out(clouds, kubernetes, disks) else 0


def something_left_out(clouds: Clouds, kubernetes: KubernetesResult, disks: DisksResult) -> bool:
    """Tell whether anything couldn't be read or priced: a cloud, a scope, a cluster, a machine or a VM."""
    unread = clouds.unread or kubernetes.unread or kubernetes.not_assessed or disks.unread or disks.not_assessed
    machines = any(unpriced(cluster) for cluster in kubernetes.clusters)
    vms = any(result.current is None for result in disks.vms)
    return bool(unread) or machines or vms


def unpriced(cluster: ClusterResult) -> List[LineResult]:
    """Give a cluster's lines whose machines have no list price; a line with no machines today needs none."""
    return [line for line in cluster.lines if line.hourly is None and line.today > 0]


def end_lines(figures: Summary, path: str, shell: Optional[CloudShell]) -> List[str]:
    """Give the lines that end a run with a report: the cost today, the saving, where the report is and what next."""
    return [
        "",
        f"Cost today: {usd(figures.today)} a month, at list prices",
        f"Saving: {usd(figures.saving)} a month ({figures.share}%)",
        f"Report: {path}",
        *(line.format(path=path) for line in DOWNLOAD[shell]),
        f"Please email it to {CONTACT}.",
    ]


def end(runtime: Runtime, started: StartedCommands, opening: str, code: int, *, contact: bool = False) -> NoReturn:
    """End the run at once: end its programs, remove its temporary folder, write the note and exit with code.

    The note says what had started in the user's cloud, so the user knows what to look for in its logs.
    """
    runtime.runner.end_children()
    parts = [opening, started.note()]
    unremoved = remove_temp(runtime.temp_root)
    if unremoved is not None:
        parts.append(unremoved)
    if contact:
        parts.append(f"Please send this line to {CONTACT}.")
    runtime.console.note(" ".join(parts))
    runtime.exit_now(code)


def remove_temp(folder: str) -> Optional[str]:
    """Remove the run's temporary folder; give why it couldn't be removed, or None."""
    # A stop can come after the run has removed the folder itself.
    if not os.path.exists(folder):
        return None
    try:
        shutil.rmtree(folder)
    except OSError as error:
        return f"The temporary folder {folder} could not be removed ({error.strerror}); remove it yourself."
    return None


def location(error: BaseException) -> str:
    """Name an error's type and the innermost line of the package it passed through, such as zoorik/cli.py:120."""
    frames = [frame for frame in traceback.extract_tb(error.__traceback__) if frame.filename.startswith(PACKAGE)]
    # run() itself is the outermost frame, so there is always one in the package.
    frame = frames[-1]
    path = os.path.relpath(frame.filename, os.path.dirname(PACKAGE)).replace(os.sep, "/")
    return f"{type(error).__name__} at {path}:{frame.lineno}"


if __name__ == "__main__":
    main()
