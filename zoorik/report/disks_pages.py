"""The Disks pages: each account with a VM in the saving, the largest saving first, one card each.

A VM's row gives its size and the data disks it has, a bar of the disk space paid for today, used and after
rightsizing, and its disk cost a month today and after.
"""

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from zoorik.cloud.keys import CLOUD_NAMES
from zoorik.disks.model import Disk, DisksResult, Saving, VmResult, tally
from zoorik.format import count, usd
from zoorik.report.blocks import (
    CARD_GAP,
    GAP,
    SECTION_GAP,
    Card,
    Cell,
    Column,
    Key,
    Legend,
    Name,
    Report,
    Row,
    Section,
    Table,
)
from zoorik.report.figures import ARROW, DOT, TIMES, pill, saving_cell, size_name, space
from zoorik.report.names import Kind, Names
from zoorik.report.pdf import BLUE, MINT, RULE, Box, Pdf

VM_COLUMNS = (Column(0.24), Column(0.26), Column(0.16, "r"), Column(0.11, "r"), Column(0.11, "r"), Column(0.12, "r"))
LEGEND = (Key("Disk space you pay for today", RULE), Key("Used", BLUE), Key("After rightsizing", MINT, dashed=True))


@dataclass(frozen=True)
class SpaceBar:
    """Art: the disk space paid for today as a grey track, the space used in blue, and the size after as dashes."""

    paid: float
    used: float
    after: float

    def height(self, width: float) -> float:
        """Give the bar's height, which is the same at any width."""
        return 7.5

    def draw(self, pdf: Pdf, x: float, y: float, width: float) -> None:
        """Draw the bar width points wide from its top-left corner (x, y); the larger of today and after fills it."""
        top = max(self.paid, self.after)
        pdf.rect(Box(x, y, width * self.paid / top, 7.5), RULE, radius=3.75)
        pdf.rect(Box(x, y, width * self.used / top, 7.5), BLUE, radius=3.75)
        pdf.dashed(Box(x, y - 1.5, width * self.after / top, 10.5), 4.5)


def disks_pages(report: Report, number: int, disks: DisksResult, names: Names) -> None:
    """Add the Disks section, numbered number: each account with a VM in the saving, the largest saving first."""
    accounts: Dict[Tuple[str, str], List[VmResult]] = {}
    for result in disks.in_saving:
        accounts.setdefault((result.vm.cloud, result.vm.account), []).append(result)
    named = [
        (names.account(cloud, account, disks.subscriptions), cloud, group)
        for (cloud, account), group in accounts.items()
    ]
    named.sort(key=lambda each: (-tally(each[2]).saving, each[0]))
    report.add(Section(number, "Disks"), GAP, keep=True)
    report.add(Legend(LEGEND), SECTION_GAP, keep=True)
    for index, (name, cloud, group) in enumerate(named):
        report.add(account_card(name, cloud, group, names), CARD_GAP if index else SECTION_GAP)


def account_card(name: str, cloud: str, group: Sequence[VmResult], names: Names) -> Card:
    """Give an account's card: its cloud, regions, VMs and disk space, its saving, and each VM, the largest first."""
    total = tally(group)
    meta = DOT.join(
        [
            CLOUD_NAMES[cloud],
            regions(group),
            count(len(group), "VM"),
            f"{space(total.paid)} {ARROW} {space(total.pool)}",
        ]
    )
    vms = sorted(group, key=lambda result: (-tally([result]).saving, names.name(Kind.VM, result.vm.name)))
    head = ["VM", "Disk space", f"Used\npaid for {ARROW} after", "Today", "After", "Saving"]
    table = Table(VM_COLUMNS, head, [vm_row(result, names) for result in vms])
    return Card.of(table, Name(name, meta, pill(total.saving, total.today)))


def vm_row(result: VmResult, names: Names) -> Row:
    """Give a VM's row: its name, size and disks, its disk space today, used and after, and its cost."""
    saving, used = sized(result)
    figures = tally([result])
    disks = disk_sizes([*saving.pooled, *saving.staying])
    name = Cell(names.name(Kind.VM, result.vm.name), f"{size_name(result.vm.cloud, result.vm.size)}{DOT}{disks}", True)
    space_used = Cell(f"{space(used)} used", f"{space(figures.paid)} {ARROW} {space(figures.pool)}")
    bar = Cell(art=SpaceBar(figures.paid, used, figures.pool))
    return [name, bar, space_used, usd(figures.today), usd(figures.after), saving_cell(figures.saving)]


def sized(result: VmResult) -> Tuple[Saving, float]:
    """Give a VM's saving and the GB used on the mounts its pool takes."""
    saving, used = result.saving, result.used_gb
    # A VM is in the saving only once its pool was sized on its used space.
    assert saving is not None and used is not None
    return saving, used


def regions(group: Sequence[VmResult]) -> str:
    """Name the VMs' region, or count the regions they are in."""
    found = sorted({result.vm.region for result in group})
    return found[0] if len(found) == 1 else count(len(found), "region")


def disk_sizes(disks: Sequence[Disk]) -> str:
    """Write the disks by size, largest first, such as 4 x 1.0 TB, or 2 x 512 GB + 1 x 128 GB."""
    sizes: Dict[int, int] = {}
    for disk in disks:
        sizes[disk.size_gb] = sizes.get(disk.size_gb, 0) + 1
    return " + ".join(f"{number} {TIMES} {space(size)}" for size, number in sorted(sizes.items(), reverse=True))
