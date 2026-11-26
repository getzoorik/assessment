"""The report on the parts' results: page 1's sums, rows and headline, and why a cluster's steps weren't all counted."""

import unittest
from dataclasses import replace

from tests.replay.test_disks import result_of as disks_of
from tests.replay.test_kubernetes import result_of as clusters_of
from zoorik.cloud.keys import AZURE
from zoorik.kubernetes.model import WorkloadResult
from zoorik.report.blocks import Cell
from zoorik.report.closing import cluster_issues
from zoorik.report.figures import saving_cell
from zoorik.report.kubernetes_pages import NO_ROOM, RAISED, pool_row, workload_row
from zoorik.report.names import Names
from zoorik.report.summary import DISKS_ROW, Row, Summary, saved_by, summary

NAMES = Names("0123456789abcdef", anonymize=False)


class PageOneTest(unittest.TestCase):
    """summary and saved_by, on the parts' results for made-up estates."""

    def test_today_is_the_machines_of_the_clusters_plus_the_disks(self) -> None:
        """The cost today is the Kubernetes total plus DisksResult.today: $730 + $200, and $372 + $24 after."""
        clusters, disks = clusters_of("aks-public"), disks_of("linux-vm")
        figures = summary(clusters, disks)
        self.assertEqual((figures.today, figures.after), (clusters.today + disks.today, clusters.after + disks.after))
        self.assertEqual((figures.today, figures.after, figures.saving, figures.share), (930, 396, 534, 57))

    def test_the_rows_add_up_to_the_saving(self) -> None:
        """Each row the steps count in, then disks: together exactly the saving."""
        for kubernetes, disk in (
            ("aks-public", "linux-vm"),
            ("eks-node-kinds", "ebs-types"),
            ("aks-private", "declined"),
        ):
            with self.subTest(kubernetes=kubernetes, disks=disk):
                figures = summary(clusters_of(kubernetes), disks_of(disk))
                self.assertEqual(sum(row.saving for row in figures.rows), figures.saving)

    def test_a_part_with_no_cost_today_has_no_row(self) -> None:
        """A stopped cluster costs nothing today, so only the disks row applies."""
        figures = summary(clusters_of("aks-stopped"), disks_of("linux-vm"))
        self.assertEqual(figures.rows, (Row(DISKS_ROW, 176),))

    def test_the_share_rounds_halves_up(self) -> None:
        """$1 of $8 is 12.5%, written 13%; rounding halves to even would write 12%."""
        self.assertEqual(Summary(8, 7, (Row(DISKS_ROW, 1),)).share, 13)

    def test_the_headline_names_only_the_rows_that_saved_something(self) -> None:
        """A private cluster that saves nothing beside a VM that saves: disks alone; with the public one: all three."""
        self.assertEqual(saved_by(summary(clusters_of("aks-private"), disks_of("linux-vm")).rows), "disks")
        every = saved_by(summary(clusters_of("aks-public"), disks_of("linux-vm")).rows)
        self.assertEqual(every, "smaller pods and spare machines (packing included), cheaper machine types, and disks")


class KubernetesRowsTest(unittest.TestCase):
    """A node pool's change and a workload's freed cells, on the Kubernetes pages."""

    def test_a_pool_that_costs_more_after_shows_the_rise(self) -> None:
        """A pool a step adds or grows costs more after: +$146, not $0, so its column adds up to all pools'."""
        line = clusters_of("aks-public").clusters[0].lines[0]
        grown = replace(line, cost_after=line.cost_today + 146)
        same = replace(line, cost_after=line.cost_today)
        smaller = replace(line, cost_after=line.cost_today - 146)
        changes = [pool_row(Cell("apps"), [each], AZURE)[4] for each in (grown, same, smaller)]
        self.assertEqual(changes, [Cell("+$146"), Cell("$0"), saving_cell(146)])

    def test_a_raised_or_held_workload_frees_nothing_and_says_why(self) -> None:
        """A raised workload asks for more than today; a held one keeps today's requests: its machines have no room."""
        raised = WorkloadResult("Deployment", "shop", "api", 0, 0, True, False, 0)
        held = WorkloadResult("Deployment", "shop", "web", 0, 0, False, True, 0)
        self.assertEqual(workload_row(raised, NAMES)[1:3], [RAISED, RAISED])
        self.assertEqual(workload_row(held, NAMES)[1:3], [NO_ROOM, NO_ROOM])


class NotInTheSavingTest(unittest.TestCase):
    """cluster_issues: why a cluster's steps weren't all counted, on its row of "Not in the saving yet"."""

    def test_workloads_kept_at_todays_requests_are_counted_with_why(self) -> None:
        """Two workloads the machines have no room to raise keep today's requests, and the row says so."""
        cluster = clusters_of("aks-public").clusters[0]
        held = [WorkloadResult("Deployment", "shop", name, 0, 0, False, True, 0) for name in ("api", "web")]
        self.assertEqual(
            cluster_issues(replace(cluster, workloads=tuple(held))),
            "2 workloads kept at today's requests: the machines have no room to raise them",
        )
