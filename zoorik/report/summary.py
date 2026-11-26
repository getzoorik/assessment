"""Page 1: the cost a month today of everything the run read, the cost after, the saving and its share, and its rows.

The cost today is the priced machines of every assessed cluster plus every priced VM data disk, each counted once. The
rows that apply follow: each row the Kubernetes steps count in, in the steps' order, then disks, each with the
assumption its figure rests on. They add up to the saving exactly. The headline names the saving and only the rows
that saved something; it credits no product.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Sequence, Tuple

from zoorik.disks.model import DisksResult
from zoorik.format import count, usd
from zoorik.kubernetes.model import KubernetesResult
from zoorik.report.blocks import GAP, Card, Cell, Column, Kpi, Kpis, Report, Table, Title, headline, text
from zoorik.report.figures import assumption, less, percent, saving_cell
from zoorik.report.pdf import GREEN_INK, MM, MUTED, Span, Style

DISKS_ROW = "disks"
LIST_PRICES = "Public list prices, pay-as-you-go, USD, before any discount or commitment."
MONTHS_A_YEAR = 12


@dataclass(frozen=True)
class Row:
    """A page-1 row: a kind of change, what it saves a month, in whole dollars, and the assumption it rests on."""

    name: str
    saving: int
    # "" for a row that rests on none.
    assumption: str


@dataclass(frozen=True)
class Summary:
    """Page 1's figures: the cost a month today and after, in whole dollars, and the rows that apply."""

    today: int
    after: int
    rows: Tuple[Row, ...]

    @property
    def saving(self) -> int:
        """Give the saving a month."""
        return self.today - self.after

    @property
    def share(self) -> int:
        """Give the saving's share of the cost today, in whole percent, halves up."""
        return percent(self.saving, self.today)


def summary(kubernetes: KubernetesResult, disks: DisksResult) -> Summary:
    """Add up both parts; each part's rows apply when it has a cost today."""
    rows: Dict[str, int] = {}
    assumptions: Dict[str, str] = {}
    if kubernetes.today > 0:
        for cluster in kubernetes.clusters:
            for step in cluster.steps:
                rows[step.row] = rows.get(step.row, 0) + step.saving
                assumptions[step.row] = step.assumption
    if disks.today > 0:
        rows[DISKS_ROW] = disks.saving
        assumptions[DISKS_ROW] = ""
    return Summary(
        today=kubernetes.today + disks.today,
        after=kubernetes.after + disks.after,
        rows=tuple(Row(name, saving, assumptions[name]) for name, saving in rows.items()),
    )


def saved_by(rows: Sequence[Row]) -> str:
    """Name the rows that saved something as a list, such as A, B, and C.

    The comma before the last one keeps the and inside a row's name, as in smaller pods and spare machines, apart.
    """
    names = [row.name for row in rows if row.saving > 0]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


def window(minutes: int) -> str:
    """Name the usage sample window: the minutes of samples, or the one sample that 0 minutes takes."""
    return "one sample" if minutes == 0 else f"{count(minutes, 'minute')} of samples"


def disclosure(kubernetes: KubernetesResult, disks: DisksResult, started: datetime, minutes: int) -> str:
    """Say what the cost today holds and the list prices it is at, and, when clusters count in it, the usage window."""
    held: List[str] = []
    if kubernetes.today > 0:
        clusters = sum(1 for cluster in kubernetes.clusters if cluster.today > 0)
        held.append(f"the priced machines of {count(clusters, 'cluster')} ({usd(kubernetes.today)})")
    if disks.today > 0:
        held.append(f"the priced data disks of {count(len(disks.vms), 'VM')} ({usd(disks.today)})")
    parts = [f"Cost today: {' and '.join(held)}, each cost counted once.", LIST_PRICES]
    if kubernetes.today > 0:
        parts.append(f"Usage: peaks from {window(minutes)}, {started.day} {started:%B %Y %H:%M} UTC.")
    return " ".join(parts)


def page_one(report: Report, figures: Summary, disclosed: str) -> None:
    """Add page 1: the headline, the figures, the rows that apply, and what the cost today holds."""
    if figures.saving > 0:
        saving = Span(f"{usd(figures.saving)} a month ({figures.share}%)", True, GREEN_INK)
        report.add(headline(["You could reduce your cloud cost by ", saving, f": {saved_by(figures.rows)}"]))
    else:
        report.add(headline("No saving found yet"))
    yearly = f"about {usd(MONTHS_A_YEAR * figures.saving)} a year"
    kpis = [
        Kpi(usd(figures.today), "cost a month,\ntoday"),
        Kpi(usd(figures.after), "cost a month,\nafter"),
        Kpi(less(figures.saving), f"saving a month,\n{yearly}", GREEN_INK),
        Kpi(f"{figures.share}%", "of the cost\ntoday", GREEN_INK),
    ]
    report.add(Kpis(kpis), 6 * MM)
    rows = [
        [Cell(row.name[:1].upper() + row.name[1:], assumption(row.assumption)), saving_cell(row.saving)]
        for row in figures.rows
    ]
    table = Table([Column(0.72), Column(0.28, "r")], ["Change", "Saving"], rows, ["Total", saving_cell(figures.saving)])
    report.add(Card.of(table, Title("Where the saving comes from", "a month, at list prices")), GAP)
    report.add(text(disclosed, Style(8.25, MUTED), 1.5), 3 * MM)
