"""The Kubernetes pages: each cluster with a cost today, the largest saving first, in up to three cards.

The first card names the cluster and gives each step's cost a month before and after it. The second gives each node
pool's machines today and after, as a mix of machine types, and their cost. The third gives what each workload frees
and its share of the row the steps split.
"""

from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from zoorik.cloud.keys import PLATFORMS
from zoorik.format import count, usd
from zoorik.kubernetes.model import ClusterResult, KubernetesResult, LineResult, WorkloadResult
from zoorik.kubernetes.steps import SHARED_ROW
from zoorik.pricing.machines import Capacity
from zoorik.report.blocks import (
    CARD_GAP,
    GAP,
    SECTION_GAP,
    Card,
    Cell,
    Column,
    Name,
    Report,
    Row,
    Section,
    Table,
    Title,
)
from zoorik.report.figures import ARROW, DOT, TIMES, change_cell, cores, gib, pill, saving_cell, size_name
from zoorik.report.names import Kind, Names
from zoorik.report.pdf import MUTED

# The workloads a cluster's table names one by one; the others share one row.
TOP_WORKLOADS = 6
STEP_COLUMNS = (Column(0.52), Column(0.16, "r"), Column(0.16, "r"), Column(0.16, "r"))
POOL_COLUMNS = (Column(0.26), Column(0.38), Column(0.12, "r"), Column(0.12, "r"), Column(0.12, "r"))
WORKLOAD_COLUMNS = (Column(0.46), Column(0.18, "r"), Column(0.18, "r"), Column(0.18, "r"))
RAISED = Cell("raised", color=MUTED)
# A held workload's freed cells: its machines have no room to raise it.
NO_ROOM = Cell("no room", color=MUTED)


def kubernetes_pages(report: Report, number: int, kubernetes: KubernetesResult, names: Names) -> None:
    """Add the Kubernetes section, numbered number: each cluster with a cost today, the largest saving first."""
    shown = [cluster for cluster in kubernetes.clusters if cluster.today > 0]
    shown.sort(key=lambda cluster: (-cluster.saving, names.name(Kind.CLUSTER, cluster.cluster.name)))
    report.add(Section(number, "Kubernetes"), GAP, keep=True)
    for index, cluster in enumerate(shown):
        report.add(steps_card(cluster, kubernetes.subscriptions, names), GAP if index else SECTION_GAP)
        report.add(pools_card(cluster, names), CARD_GAP)
        workloads = workloads_card(cluster, names)
        if workloads is not None:
            report.add(workloads, CARD_GAP)


def steps_card(cluster: ClusterResult, subscriptions: Mapping[str, str], names: Names) -> Card:
    """Give a cluster's first card: its name, platform, region, account and machines, and each step's cost."""
    where = cluster.cluster
    account = names.account(where.cloud, where.account, subscriptions)
    meta = DOT.join([PLATFORMS[where.cloud], where.region, account, machines(cluster.lines)])
    heading = Name(names.name(Kind.CLUSTER, where.name), meta, pill(cluster.saving, cluster.today))
    rows: List[Row] = [
        [step.name, usd(step.before), usd(step.after), saving_cell(step.saving)] for step in cluster.steps
    ]
    total: Row = ["All steps", usd(cluster.today), usd(cluster.after), saving_cell(cluster.saving)]
    return Card.of(Table(STEP_COLUMNS, ["Step", "Before", "After", "Saving"], rows, total), heading)


def pools_card(cluster: ClusterResult, names: Names) -> Card:
    """Give a cluster's node pools: each one's machines today and after, and their cost; a total when there are two."""
    pools: Dict[Tuple[str, str], List[LineResult]] = {}
    for line in cluster.lines:
        pools.setdefault((line.kind, line.pool), []).append(line)
    rows = [
        pool_row(Cell(names.name(Kind.POOL, pool, cluster.cluster.name), kind, bold=True), lines, cluster.cluster.cloud)
        for (kind, pool), lines in pools.items()
    ]
    total: Optional[Row] = None
    if len(rows) > 1:
        total = [
            "All pools",
            machines(cluster.lines),
            usd(cluster.today),
            usd(cluster.after),
            saving_cell(cluster.saving),
        ]
    head = ["Node pool", f"Machines\ntoday {ARROW} after", "Today", "After", "Change"]
    return Card.of(Table(POOL_COLUMNS, head, rows, total), Title("Node pools", "a month, at list prices"))


def pool_row(name: Cell, lines: Sequence[LineResult], cloud: str) -> Row:
    """Give a pool's row: its machines today and after, and their cost; a line with no price counts in neither."""
    mix = f"{fleet(lines, cloud, today=True)} {ARROW} {fleet(lines, cloud, today=False)}"
    unpriced = [line for line in lines if line.hourly is None]
    if len(unpriced) == len(lines):
        return [name, mix, Cell("not priced", color=MUTED), "", ""]
    today = sum(line.cost_today for line in lines)
    after = sum(line.cost_after for line in lines)
    # The pool's figures are its priced machines' alone, as the cluster's totals are.
    cost = Cell(usd(today), "not all priced" if unpriced else "")
    return [name, mix, cost, usd(after), change_cell(today, after)]


def machines(lines: Sequence[LineResult]) -> str:
    """Write how many machines lines hold today and after, such as 5 -> 3 machines."""
    return f"{sum(line.today for line in lines)} {ARROW} {count(sum(line.after for line in lines), 'machine')}"


def fleet(lines: Sequence[LineResult], cloud: str, *, today: bool) -> str:
    """Write a pool's machines today, or after, as a mix such as 3 x D4s v5 + 1 x D8s v5 spot; 0 when it has none."""
    parts = []
    for line in lines:
        number = line.today if today else line.after
        if number:
            parts.append(line_machines(line, number, cloud))
    return " + ".join(parts) if parts else "0"


def line_machines(line: LineResult, number: int, cloud: str) -> str:
    """Write number machines of a line: by their type and, on spot, so; by count alone when the type isn't known."""
    if line.machine_type is None:
        return count(number, "machine")
    machine = f"{number} {TIMES} {size_name(cloud, line.machine_type)}"
    return f"{machine} spot" if line.capacity == Capacity.SPOT.value else machine


def workloads_card(cluster: ClusterResult, names: Names) -> Optional[Card]:
    """Give the workloads that free CPU or memory, or were raised or held, the largest share first; None for none."""
    listed = [
        workload
        for workload in cluster.workloads
        if workload.cpu or workload.memory or workload.raised or workload.held
    ]
    if not listed:
        return None
    listed.sort(key=lambda each: (-each.share, -each.cpu, -each.memory, each.namespace, each.name))
    rows = [workload_row(workload, names) for workload in listed[:TOP_WORKLOADS]]
    others = listed[TOP_WORKLOADS:]
    if others:
        rows.append(sum_row(Cell(count(len(others), "other workload"), color=MUTED), others))
    total = sum_row(Cell(f"All {count(len(listed), 'workload')}"), listed) if len(listed) > 1 else None
    head = ["Workload", "CPU freed\ncores", "Memory freed\nGiB", "Share\na month"]
    return Card.of(Table(WORKLOAD_COLUMNS, head, rows, total), Title("Workloads", f"share of {SHARED_ROW}"))


def workload_row(workload: WorkloadResult, names: Names) -> Row:
    """Give a workload's row: its name over its namespace and kind, what it frees or why it frees nothing, its share."""
    namespace = names.name(Kind.NAMESPACE, workload.namespace)
    name = Cell(names.name(Kind.WORKLOAD, workload.name, workload.namespace), f"{namespace}{DOT}{workload.kind}", True)
    if workload.held:
        freed: Row = [NO_ROOM, NO_ROOM]
    elif workload.raised:
        freed = [RAISED, RAISED]
    else:
        freed = [cores(workload.cpu), gib(workload.memory)]
    return [name, *freed, saving_cell(workload.share)]


def sum_row(first: Cell, workloads: Sequence[WorkloadResult]) -> Row:
    """Give a row of what workloads free in all, and their shares in all, under the first cell."""
    cpu = sum(workload.cpu for workload in workloads)
    memory = sum(workload.memory for workload in workloads)
    return [first, cores(cpu), gib(memory), saving_cell(sum(workload.share for workload in workloads))]
