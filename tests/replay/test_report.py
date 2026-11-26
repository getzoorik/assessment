"""The report on the parts' results: page 1's sums, rows and headline, and why a cluster's steps weren't all counted."""

import unittest
from dataclasses import replace

from tests.replay.test_disks import result_of as disks_of
from tests.replay.test_kubernetes import ACCESS_DENIED
from tests.replay.test_kubernetes import result_of as clusters_of
from zoorik.cloud.keys import AZURE
from zoorik.kubernetes.model import WorkloadResult
from zoorik.kubernetes.steps import SHARED_ROW, fewer_copies, spot
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
        """The cost today is the Kubernetes total plus DisksResult.today: $730 + $200, and $248 + $24 after."""
        clusters, disks = clusters_of("aks-public"), disks_of("linux-vm")
        figures = summary(clusters, disks)
        self.assertEqual((figures.today, figures.after), (clusters.today + disks.today, clusters.after + disks.after))
        self.assertEqual((figures.today, figures.after, figures.saving, figures.share), (930, 272, 658, 71))

    def test_the_rows_add_up_to_the_saving(self) -> None:
        """Each row the steps count in, then disks: together exactly the saving."""
        for kubernetes, disk in (
            ("aks-public", "linux-vm"),
            ("aks-quiet-hours", "linux-vm"),
            ("eks-node-kinds", "ebs-types"),
            ("aks-private", "declined"),
        ):
            with self.subTest(kubernetes=kubernetes, disks=disk):
                figures = summary(clusters_of(kubernetes), disks_of(disk))
                self.assertEqual(sum(row.saving for row in figures.rows), figures.saving)

    def test_a_part_with_no_cost_today_has_no_row(self) -> None:
        """A stopped cluster costs nothing today, so only the disks row applies."""
        figures = summary(clusters_of("aks-stopped"), disks_of("linux-vm"))
        self.assertEqual(figures.rows, (Row(DISKS_ROW, 176, ""),))

    def test_the_rows_of_fewer_copies_and_spot_state_their_assumptions(self) -> None:
        """Steps 5 and 6 each rest on an assumption, which their rows carry; the other rows rest on none."""
        figures = summary(clusters_of("aks-quiet-hours"), disks_of("linux-vm"))
        self.assertEqual(
            figures.rows,
            (
                Row(SHARED_ROW, 292, ""),
                Row("cheaper machine types", 0, ""),
                Row(fewer_copies.ROW, 73, fewer_copies.ASSUMPTION),
                Row(spot.ROW, 175, spot.ASSUMPTION),
                Row(DISKS_ROW, 176, ""),
            ),
        )

    def test_the_share_rounds_halves_up(self) -> None:
        """$1 of $8 is 12.5%, written 13%; rounding halves to even would write 12%."""
        self.assertEqual(Summary(8, 7, (Row(DISKS_ROW, 1, ""),)).share, 13)

    def test_the_headline_names_only_the_rows_that_saved_something(self) -> None:
        """A private cluster that saves nothing, beside a VM that saves: disks alone; the public one adds three rows.

        The quiet-hours cluster saves in every row but cheaper machine types.
        """
        self.assertEqual(saved_by(summary(clusters_of("aks-private"), disks_of("linux-vm")).rows), "disks")
        public = saved_by(summary(clusters_of("aks-public"), disks_of("linux-vm")).rows)
        self.assertEqual(
            public,
            "smaller pods and spare machines (packing included), cheaper machine types, fewer copies at quiet times, "
            "and disks",
        )
        quiet = saved_by(summary(clusters_of("aks-quiet-hours"), disks_of("linux-vm")).rows)
        self.assertEqual(
            quiet, "smaller pods and spare machines (packing included), fewer copies at quiet times, spot, and disks"
        )


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

    def test_unread_budgets_and_machines_with_no_spot_price_are_named_with_why(self) -> None:
        """Fewer copies weren't counted, as the budgets couldn't be read, and apps' 4 machines have no spot price."""
        cluster = clusters_of("aks-quiet-hours-not-counted").clusters[0]
        self.assertEqual(
            cluster_issues(cluster),
            f"fewer copies at quiet times not counted: cannot list poddisruptionbudgets.policy: {ACCESS_DENIED}; "
            "4 machines not on spot at quiet times: no spot price for Standard_D4s_v5 in westeurope",
        )

    def test_a_cluster_with_every_step_counted_names_nothing(self) -> None:
        """The quiet-hours cluster read in full, on spot at quiet times, has no reason to give."""
        self.assertEqual(cluster_issues(clusters_of("aks-quiet-hours").clusters[0]), "")
