"""Step 5, fewer copies at quiet times: the figure worked out by hand, the apps that stay as they are, the limits."""

import unittest
from dataclasses import replace
from typing import Dict, Optional, Sequence, Tuple

from tests.unit.fleet import APPS, GIB, NAMESPACE, api_pod, machine, placed, pool, state
from zoorik.kubectl.api import Scaling
from zoorik.kubectl.objects import ADDON_MODE, Autoscaler, DisruptionBudget, LabelSelector
from zoorik.kubernetes.assessment import app_results, line_results
from zoorik.kubernetes.model import AppResult, ClusterState, Pod, Workload
from zoorik.kubernetes.steps import Pipeline, cost
from zoorik.kubernetes.steps.fewer_copies import ASSUMPTION, ROW, FewerCopies, minimum_copies
from zoorik.kubernetes.steps.smaller_pods import RECONCILE

WEB = Workload("Deployment", NAMESPACE, "web")
NOTHING = Scaling((), (), None)
FRONT = LabelSelector({"tier": "front"}, ())


def app(
    copies: int,
    cpu: int = 1500,
    workload: Workload = WEB,
    labels: Optional[Dict[str, str]] = None,
    per_machine: int = 2,
) -> ClusterState:
    """Make a pool of machines at $0.20 an hour holding an app's copies, per_machine on each, each asking cpu."""
    machines = [machine(f"m{index}") for index in range(1, (copies + per_machine - 1) // per_machine + 1)]
    pods = [
        placed(
            api_pod(f"{workload.name}-{index}", cpu, GIB, labels=labels or {"tier": "front"}),
            f"m{index // per_machine + 1}",
            workload=workload,
        )
        for index in range(copies)
    ]
    return state([pool()], machines, pods)


def quiet_copies(after: ClusterState) -> Tuple[int, int]:
    """Count the machines and the pods at quiet times."""
    assert after.quiet is not None
    return len(after.quiet.machines), len(after.quiet.pods)


def budget(
    min_available: Optional[int], selector: Optional[LabelSelector] = FRONT, namespace: str = NAMESPACE
) -> Scaling:
    """Give the scaling of a cluster with one disruption budget."""
    return Scaling((), (DisruptionBudget(namespace, selector, min_available),), None)


class WorkedFigureTest(unittest.TestCase):
    """The figure worked out by hand."""

    def test_an_app_of_4_copies_drops_to_2_and_frees_a_machine_for_12_hours(self) -> None:
        """Two machines at $0.20 hold 4 copies of 1.5 cores; 2 copies fit one: $292 to 730 x (0.5 x 2 + 0.5 x 1) x 0.2.

        That is $219, a saving of $73, half of one machine's month.
        """
        today = app(4)
        after, results = Pipeline([FewerCopies(NOTHING)]).run(today)
        figures = [(each.name, each.row, each.assumption, each.before, each.after, each.saving) for each in results]
        self.assertEqual(figures, [("Fewer copies at quiet times", ROW, ASSUMPTION, 292, 219, 73)])
        self.assertEqual((after.machines, after.pods), (today.machines, today.pods))
        self.assertEqual(quiet_copies(after), (1, 2))
        self.assertEqual(app_results(after), (AppResult("Deployment", NAMESPACE, "web", 4, 2),))
        lines = [
            (each.today, each.after, each.quiet, each.cost_today, each.cost_after)
            for each in line_results(today, after)
        ]
        self.assertEqual(lines, [(2, 2, 1, 292, 219)])
        self.assertEqual(
            ASSUMPTION,
            "each app with 3 or more copies and no autoscaler of its own runs at 2 copies (or its disruption "
            "budget's floor) for 12 hours a day",
        )

    def test_the_copies_kept_are_the_first_by_name(self) -> None:
        """The copies that run at quiet times are the app's first two pods by namespace and name."""
        after = FewerCopies(NOTHING).apply(app(4))
        assert after.quiet is not None
        self.assertEqual(sorted(pod.source.name for pod in after.quiet.pods), ["web-0", "web-1"])


class StaysTest(unittest.TestCase):
    """The apps that stay as they are."""

    def test_an_app_with_its_own_autoscaler_stays(self) -> None:
        """An HPA whose scale target is the app, KEDA's included, keeps all its copies; one of another app doesn't."""
        today = app(4)
        own = Scaling((Autoscaler(NAMESPACE, "Deployment", "web"),), (), None)
        self.assertEqual(FewerCopies(own).apply(today), today)
        others = [
            Autoscaler(NAMESPACE, "Deployment", "api"),
            Autoscaler(NAMESPACE, "StatefulSet", "web"),
            Autoscaler("other", "Deployment", "web"),
        ]
        self.assertEqual(quiet_copies(FewerCopies(Scaling(tuple(others), (), None)).apply(today)), (1, 2))

    def test_an_app_of_2_copies_a_statefulset_and_a_reconciled_app_stay(self) -> None:
        """2 copies have none to spare; StatefulSets, DaemonSets, Jobs and an app the platform resets stay too."""
        cases = {
            "2 copies": app(2),
            "StatefulSet": app(4, workload=Workload("StatefulSet", NAMESPACE, "db")),
            "DaemonSet": app(4, workload=Workload("DaemonSet", NAMESPACE, "agent")),
            "Job": app(4, workload=Workload("Job", NAMESPACE, "report")),
            "Reconcile": app(4, labels={ADDON_MODE: RECONCILE}),
        }
        for name, today in cases.items():
            with self.subTest(name):
                self.assertEqual(FewerCopies(NOTHING).apply(today), today)

    def test_an_argo_rollout_is_an_app(self) -> None:
        """A Rollout's copies drop to 2 as a Deployment's do."""
        after = FewerCopies(NOTHING).apply(app(4, workload=Workload("Rollout", NAMESPACE, "web")))
        self.assertEqual(quiet_copies(after), (1, 2))

    def test_an_app_whose_budget_floor_is_at_or_above_its_copies_stays(self) -> None:
        """A budget's minAvailable of 3 on 4 copies makes the minimum 4, so the app keeps every copy."""
        today = app(4)
        self.assertEqual(FewerCopies(budget(3)).apply(today), today)

    def test_a_cluster_whose_autoscalers_or_budgets_were_not_read_gets_no_step_5(self) -> None:
        """Either could hold any app at its copies, so no app is counted."""
        today = app(4)
        self.assertEqual(
            FewerCopies(Scaling((), (), "cannot list poddisruptionbudgets.policy: access denied")).apply(today), today
        )

    def test_copies_that_free_no_machine_leave_the_state_as_it_was(self) -> None:
        """4 copies of 0.5 cores share one machine, so 2 copies at quiet times free none, and nothing is counted."""
        today = app(4, 500, per_machine=4)
        self.assertEqual(FewerCopies(NOTHING).apply(today), today)


class MinimumTest(unittest.TestCase):
    """minimum_copies: 2, or one more than the largest whole-number floor of a budget that selects the app."""

    def copies(self, number: int) -> Sequence[Pod]:
        """Give an app's copies, each labelled tier: front."""
        return app(number, 100, per_machine=number).pods

    def test_the_minimum_is_2_or_one_more_than_a_budgets_floor(self) -> None:
        """A minAvailable of 1 keeps 2, one of 2 keeps 3, and the largest of two budgets wins."""
        pods = self.copies(6)
        self.assertEqual(minimum_copies(pods, ()), 2)
        self.assertEqual(minimum_copies(pods, budget(1).budgets), 2)
        self.assertEqual(minimum_copies(pods, budget(2).budgets), 3)
        self.assertEqual(minimum_copies(pods, (*budget(1).budgets, *budget(4).budgets)), 5)

    def test_a_percentage_or_max_unavailable_and_a_budget_of_other_pods_raise_nothing(self) -> None:
        """A budget with no whole-number floor, one in another namespace and one selecting other labels keep 2."""
        pods = self.copies(6)
        budgets = [
            *budget(None).budgets,
            *budget(4, namespace="other").budgets,
            *budget(4, LabelSelector({"tier": "back"}, ())).budgets,
            *budget(4, None).budgets,
        ]
        self.assertEqual(minimum_copies(pods, budgets), 2)

    def test_an_empty_selector_selects_every_pod_of_its_namespace(self) -> None:
        """A budget whose selector is empty selects the app's pods, so its floor counts."""
        self.assertEqual(minimum_copies(self.copies(6), budget(3, LabelSelector({}, ())).budgets), 4)

    def test_an_app_keeps_every_copy_when_it_has_none_to_spare(self) -> None:
        """Fewer than 3 copies, or a minimum at or above the copies, keeps them all."""
        self.assertEqual(minimum_copies(self.copies(2), ()), 2)
        self.assertEqual(minimum_copies(self.copies(4), budget(5).budgets), 4)


class PoolsTest(unittest.TestCase):
    """The quiet hours' machines: no pool grows, and a pool may shrink to the platform's minimum."""

    def test_no_pool_grows_at_quiet_times(self) -> None:
        """A cheaper pool with no machines gets none: the 2 copies left stay on one of the app's own machines."""
        today = app(4)
        before = state([*today.pools, pool("cheap", 0.1)], today.machines, today.pods)
        after = FewerCopies(NOTHING).apply(before)
        assert after.quiet is not None
        self.assertEqual({each.pool for each in after.quiet.machines}, {APPS})
        self.assertEqual(len(after.quiet.machines), 1)

    def test_a_system_pool_keeps_one_machine_at_quiet_times(self) -> None:
        """An AKS system pool's 4 copies drop to 2 on one machine, and it keeps that one."""
        system = pool("system", system=True)
        today = app(4)
        machines = [replace(each, pool=system.id) for each in today.machines]
        pods = [replace(each, pool=system.id) for each in today.pods]
        after = FewerCopies(NOTHING).apply(state([system], machines, pods))
        self.assertEqual(quiet_copies(after), (1, 2))
        self.assertEqual(cost(after), 219)
