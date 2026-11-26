"""Step 5, fewer copies at quiet times: apps with copies to spare run fewer of them 12 hours a day.

An app is a Deployment or an Argo Rollout, and its copies are its pods the run read. An app with 3 or more copies and
no HorizontalPodAutoscaler of its own runs at its minimum for half of the hours: 2 copies, or one more than the largest
whole-number minAvailable of the PodDisruptionBudgets that select its pods, if that is higher. An app whose minimum is
at or above its copies stays as it is, and so does an app the platform puts back as it was. StatefulSets, DaemonSets
and Jobs stay as they are.

The pods left at quiet times are packed onto as few of step 4's machines as hold them, under step 4's rules; no pool
grows. Those machines run the quiet half of the hours, and step 4's the other half, all at regular prices.
"""

from dataclasses import replace
from typing import Dict, List, Sequence, Set, Tuple

from zoorik.kubectl.api import Scaling
from zoorik.kubectl.objects import ADDON_MODE, DisruptionBudget
from zoorik.kubernetes.model import ClusterState, Pod, QuietHours, Workload
from zoorik.kubernetes.placement import selected, selector_of
from zoorik.kubernetes.steps import cost
from zoorik.kubernetes.steps.pack_across_pools import across
from zoorik.kubernetes.steps.smaller_pods import RECONCILE

ROW = "fewer copies at quiet times"
ASSUMPTION = (
    "each app with 3 or more copies and no autoscaler of its own runs at 2 copies (or its disruption budget's floor) "
    "for 12 hours a day"
)
APP_KINDS = ("Deployment", "Rollout")
# An app needs at least this many copies to run fewer at quiet times.
LEAST_COPIES = 3
QUIET_COPIES = 2


class FewerCopies:
    """Runs each app with copies to spare at its minimum at quiet times, when that frees machines then."""

    name = "Fewer copies at quiet times"
    row = ROW
    assumption = ASSUMPTION

    def __init__(self, scaling: Scaling) -> None:
        """Count copies with the cluster's autoscalers and disruption budgets."""
        self._scaling = scaling

    def apply(self, state: ClusterState) -> ClusterState:
        """Give the state with its quiet hours' machines and pods; the state as it was when that saves nothing."""
        # An autoscaler or a budget the run couldn't read may hold any app at its copies, so no app is counted.
        if self._scaling.reason is not None:
            return state
        running = quiet_pods(state.pods, self._scaling)
        if len(running) == len(state.pods):
            return state
        packed = across(replace(state, pods=running), grow=False)
        if packed is None:
            return state
        quiet = replace(state, quiet=QuietHours(packed.machines, packed.pods))
        return quiet if cost(quiet) < cost(state) else state


def quiet_pods(pods: Sequence[Pod], scaling: Scaling) -> Tuple[Pod, ...]:
    """Give the pods that run at quiet times: each app with copies to spare keeps its first copies by name."""
    apps: Dict[Workload, List[Pod]] = {}
    for pod in pods:
        if pod.workload.kind in APP_KINDS:
            apps.setdefault(pod.workload, []).append(pod)
    scaled = {(each.namespace, each.kind, each.name) for each in scaling.autoscalers}
    stopped: Set[str] = set()
    for app, copies in apps.items():
        if (app.namespace, app.kind, app.name) not in scaled:
            kept = minimum_copies(copies, scaling.budgets)
            stopped.update(pod.key for pod in sorted(copies, key=lambda pod: pod.key)[kept:])
    return tuple(pod for pod in pods if pod.key not in stopped)


def minimum_copies(copies: Sequence[Pod], budgets: Sequence[DisruptionBudget]) -> int:
    """Give the copies an app keeps at quiet times: all of them, or its minimum when it has copies to spare."""
    # The platform puts back an app it manages as it was.
    if len(copies) < LEAST_COPIES or any(pod.source.labels.get(ADDON_MODE) == RECONCILE for pod in copies):
        return len(copies)
    namespace = copies[0].workload.namespace
    floors = [
        budget.min_available + 1
        for budget in budgets
        if budget.min_available is not None
        and budget.namespace == namespace
        and any(selected(selector_of(budget.selector, (), (), {}), pod.source.labels) for pod in copies)
    ]
    return min(len(copies), max([QUIET_COPIES, *floors]))
