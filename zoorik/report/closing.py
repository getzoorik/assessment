"""The report's end: "Not in the saving yet", then "Next steps", the only place the products are named.

"Not in the saving yet" names everything the run couldn't read or count, each with its reason: a cloud, another Azure
directory, a subscription or region not listed, a cluster not assessed, a cluster's machines with no list price, its
pods left in place, its workloads kept at today's requests, its nodes that are not machines, its cheaper machine types
and its fewer copies at quiet times when they can't be counted, its machines with no spot price, and each VM without a
saving.
"""

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple, Union

from zoorik import CONTACT
from zoorik.cloud.azure import OtherDirectory
from zoorik.cloud.keys import AZURE, CLOUD_NAMES, PLATFORMS
from zoorik.disks.assessment import NO_PRICE
from zoorik.disks.model import DisksResult, VmResult
from zoorik.format import count, half_up, usd
from zoorik.kubernetes.model import ClusterResult, KubernetesResult
from zoorik.report.blocks import GAP, SECTION_GAP, Card, Cell, Column, NextSteps, Report, Row, Section, Table, fine
from zoorik.report.figures import DOT, sentence
from zoorik.report.names import Kind, Names
from zoorik.report.pdf import MUTED, WHITE, Span

BOOKING = "cal.com/zoorik/talk"
FINE_PRINT = "An estimate, not a quote. The run read your accounts and sent nothing."
# Why another directory's subscriptions are not read.
OTHER = "the run reads the directory az is signed in to"
WITH_TODAY = (Column(0.2115), Column(0.6883), Column(0.1002, "r"))
WITHOUT_TODAY = (Column(0.2115), Column(0.7885))


@dataclass(frozen=True)
class CloudUnread:
    """A cloud whose CLI is installed but couldn't read, and why."""

    cloud: str
    reason: str


@dataclass(frozen=True)
class LeftOut:
    """A row of "Not in the saving yet": what was left out, a muted line under it, why, and a VM's cost today."""

    name: str
    meta: str
    why: str
    # A VM's cost today, or that it has none; None on the rows that are not VMs.
    today: Optional[Union[str, Cell]] = None


def left_out(
    kubernetes: KubernetesResult,
    disks: DisksResult,
    clouds: Sequence[CloudUnread],
    elsewhere: Sequence[OtherDirectory],
    names: Names,
) -> List[LeftOut]:
    """Give the rows of "Not in the saving yet": the clouds, the scopes, the clusters, then the VMs."""
    rows = [LeftOut(cloud.cloud, "", cloud.reason) for cloud in clouds]
    rows += [
        LeftOut("Azure", "another directory", f"{count(len(other.subscriptions), 'subscription')} not read: {OTHER}")
        for other in elsewhere
    ]
    rows += [
        scope_row(scope.cloud, scope.name, f"clusters not listed: {scope.reason}", names) for scope in kubernetes.unread
    ]
    rows += [scope_row(scope.cloud, scope.name, f"VMs not listed: {scope.reason}", names) for scope in disks.unread]
    rows += [
        LeftOut(
            names.name(Kind.CLUSTER, cluster.name), DOT.join([PLATFORMS[cluster.cloud], cluster.region]), cluster.reason
        )
        for cluster in kubernetes.not_assessed
    ]
    for cluster in kubernetes.clusters:
        issues = cluster_issues(cluster)
        if issues:
            where = cluster.cluster
            rows.append(
                LeftOut(names.name(Kind.CLUSTER, where.name), DOT.join([PLATFORMS[where.cloud], where.region]), issues)
            )
    unsaved = sorted(
        (result for result in disks.vms if result.saving is None), key=lambda result: vm_order(result, names)
    )
    rows += [vm_row(result, disks, names) for result in unsaved]
    rows += [
        LeftOut(names.name(Kind.VM, vm.name), names.account(vm.cloud, vm.account, disks.subscriptions), vm.reason)
        for vm in disks.not_assessed
    ]
    return rows


def scope_row(cloud: str, name: str, why: str, names: Names) -> LeftOut:
    """Give the row of a subscription or region whose clusters or VMs couldn't be listed."""
    if cloud == AZURE:
        return LeftOut(names.name(Kind.SUBSCRIPTION, name), "Azure subscription", why)
    return LeftOut(name, f"{CLOUD_NAMES[cloud]} region", why)


def cluster_issues(cluster: ClusterResult) -> str:
    """Say what of an assessed cluster the saving leaves out, and why; empty when nothing is."""
    issues: List[str] = []
    unpriced = [line for line in cluster.lines if line.hourly is None]
    if unpriced:
        notes = "; ".join(sorted({line.note for line in unpriced}))
        issues.append(f"{count(sum(line.today for line in unpriced), 'machine')} not priced: {notes}")
    if cluster.usage is not None:
        issues.append(f"usage not read, so its pods keep today's requests: {cluster.usage}")
    if cluster.bare_pods:
        issues.append(f"{count(cluster.bare_pods, 'pod')} with no controller left in place")
    held = sum(1 for workload in cluster.workloads if workload.held)
    if held:
        them = "it" if held == 1 else "them"
        issues.append(f"{count(held, 'workload')} kept at today's requests: the machines have no room to raise {them}")
    if cluster.not_machines:
        issues.append(f"{count(cluster.not_machines, 'node')} not counted: Fargate and virtual nodes are not machines")
    if cluster.cheaper_types is not None:
        issues.append(f"cheaper machine types not counted: {cluster.cheaper_types}")
    if cluster.fewer_copies is not None:
        issues.append(f"fewer copies at quiet times not counted: {cluster.fewer_copies}")
    regular = [line for line in cluster.lines if line.spot_note and line.quiet]
    if regular:
        notes = "; ".join(sorted({line.spot_note for line in regular}))
        issues.append(f"{count(sum(line.quiet for line in regular), 'machine')} not on spot at quiet times: {notes}")
    return "; ".join(issues)


def vm_order(result: VmResult, names: Names) -> Tuple[bool, float, str]:
    """Order VMs without a saving: the priced first, the costliest first, then by name."""
    current = result.current
    return (current is None, -(current if current is not None else 0.0), names.name(Kind.VM, result.vm.name))


def vm_row(result: VmResult, disks: DisksResult, names: Names) -> LeftOut:
    """Give the row of a VM without a saving: its reasons, its mount points hidden under --anonymize, its cost today."""
    reasons = [reason for reason in result.reasons if reason != NO_PRICE]
    if result.price_note:
        reasons.append(f"cost today unknown: {result.price_note}")
    vm = result.vm
    today = Cell("not priced", color=MUTED) if result.current is None else usd(half_up(result.current))
    account = names.account(vm.cloud, vm.account, disks.subscriptions)
    return LeftOut(names.name(Kind.VM, vm.name), account, names.mounts("; ".join(reasons)), today)


def closing(
    report: Report, number: int, rows: Sequence[LeftOut], kubernetes: KubernetesResult, disks: DisksResult
) -> None:
    """Add "Not in the saving yet", numbered number, if anything was left out; then "Next steps" and the fine print."""
    if rows:
        priced = any(row.today is not None for row in rows)
        table = Table(
            WITH_TODAY if priced else WITHOUT_TODAY,
            ["Name", "Why", "Today"] if priced else ["Name", "Why"],
            [left_out_cells(row, priced=priced) for row in rows],
        )
        report.add(Section(number, "Not in the saving yet"), GAP, keep=True)
        report.add(Card.of(table), SECTION_GAP)
    report.add(NextSteps("Next steps", next_steps(kubernetes, disks)), GAP, keep=True)
    report.add(fine(FINE_PRINT), SECTION_GAP)


def left_out_cells(row: LeftOut, *, priced: bool) -> Row:
    """Give a row's cells: its name over its muted line, why as a sentence, and its cost today in a priced table."""
    cells: List[Union[str, Cell]] = [Cell(row.name, row.meta, True), sentence(row.why)]
    if priced:
        cells.append("" if row.today is None else row.today)
    return cells


def next_steps(kubernetes: KubernetesResult, disks: DisksResult) -> List[Union[str, Span]]:
    """Give what to do next: email the PDF, book a call; then, for each part with a cost, the product that does it."""
    spans: List[Union[str, Span]] = [
        "1. Email this PDF to ",
        Span(CONTACT, True, WHITE, f"mailto:{CONTACT}"),
        ".\u00a0\u00a02. Book 30 minutes: ",
        Span(BOOKING, True, WHITE, f"https://{BOOKING}"),
        ".",
    ]
    if kubernetes.today > 0:
        spans.append("\nCoral rebalances pods and machines.")
    if disks.today > 0:
        spans.append("\nAmoeba rightsizes disks.")
    return spans
