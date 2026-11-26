"""Step 1, smaller pods: one request per workload and container, from the peaks, within limits and LimitRanges."""

import unittest
from dataclasses import replace
from fractions import Fraction
from typing import Any, Dict, Mapping, Sequence

from tests.unit.fleet import api_pod, machine, peaks, placed, pool, state
from zoorik.kubectl.objects import ADDON_MODE, Container, InitContainer, LimitItem, LimitRange, OwnerRef
from zoorik.kubectl.usage import MEBIBYTE, ContainerKey, Peak
from zoorik.kubernetes.model import ClusterState, Pod, Workload
from zoorik.kubernetes.steps import Pipeline
from zoorik.kubernetes.steps.smaller_pods import SmallerPods

WEB = Workload("Deployment", "shop", "web")
A = Workload("Deployment", "shop", "a")
C = Workload("Deployment", "shop", "c")
X = Workload("Deployment", "shop", "x")


def resized(pods: Sequence[Pod], found: Mapping[ContainerKey, Peak], ranges: Sequence[LimitRange] = ()) -> ClusterState:
    """Run smaller pods on pods, all on one machine."""
    return SmallerPods(found, ranges).apply(state([pool()], [machine("m1")], pods))


def requests(after: ClusterState) -> Dict[str, Mapping[str, int]]:
    """Give each pod's request by pod name, as the scheduler counts it."""
    return {pod.source.name: pod.request for pod in after.pods}


def with_containers(name: str, *containers: Container, **changes: Any) -> Pod:
    """Place a pod of web whose containers are those given."""
    return placed(replace(api_pod(name), containers=containers, **changes), "m1", workload=WEB)


def peak(name: str, container: str, cpu: int, memory: int) -> Dict[ContainerKey, Peak]:
    """Give one container's peak."""
    return {ContainerKey("shop", name, container): Peak(cpu, memory)}


class OneRequestPerWorkloadTest(unittest.TestCase):
    """Every replica gets the request that holds its busiest replica."""

    def test_replicas_peaking_at_0_1_0_1_and_0_7_cores_all_get_0_91(self) -> None:
        """The busiest replica peaks at 700m, so every replica asks 700m x 1.3 = 910m, and 32 MiB, the floor."""
        pods = [placed(api_pod(f"web-{n}"), "m1", workload=WEB) for n in range(1, 4)]
        found = {**peak("web-1", "app", 100, MEBIBYTE), **peak("web-2", "app", 100, MEBIBYTE)}
        found.update(peak("web-3", "app", 700, MEBIBYTE))
        after = resized(pods, found)
        self.assertEqual(
            {name: (request["cpu"], request["memory"]) for name, request in requests(after).items()},
            {
                "web-1": (910, 32 * MEBIBYTE),
                "web-2": (910, 32 * MEBIBYTE),
                "web-3": (910, 32 * MEBIBYTE),
            },
        )
        self.assertTrue(all(pod.raised for pod in after.pods))

    def test_a_raised_request_never_goes_above_the_limit(self) -> None:
        """A peak of 700m would ask 910m, but the limit is 500m, so the request is raised to 500m and no further."""
        limited = Container("app", {"cpu": 100, "memory": 64 * MEBIBYTE}, {"cpu": 500}, ())
        after = resized([with_containers("web-1", limited)], peak("web-1", "app", 700, 50 * MEBIBYTE))
        self.assertEqual((requests(after)["web-1"]["cpu"], requests(after)["web-1"]["memory"]), (500, 65 * MEBIBYTE))
        self.assertTrue(after.pods[0].raised)

    def test_a_container_with_no_reading_keeps_its_request(self) -> None:
        """No peak was read, so the pod keeps today's request and is not raised."""
        pods = [placed(api_pod("web-1", 250, 300 * MEBIBYTE), "m1", workload=WEB)]
        after = resized(pods, {})
        self.assertEqual(after.pods, tuple(pods))


class HeldTest(unittest.TestCase):
    """A workload whose raise a machine has no room for keeps today's requests, and is held."""

    def test_replicas_whose_raise_overfills_their_machine_keep_todays_requests(self) -> None:
        """Three replicas of 1000m peak at 1500m, so would ask 1950m each: 5,850m on a machine of 4,000m."""
        pods = [placed(api_pod(f"web-{n}", 1000), "m1", workload=WEB) for n in range(1, 4)]
        after = resized(pods, peaks(pods, 1500, 64 * MEBIBYTE))
        self.assertEqual(after.pods, tuple(replace(pod, held=True) for pod in pods))

    def test_a_held_workload_can_leave_another_machine_short_which_holds_that_ones_workload(self) -> None:
        """a-1 and x-1 ask 3500m of m1's 4000m today, and a-2 and c-1 3500m of m2's.

        a's replicas would ask 1300m, which puts m1 at 4300m, so a is held. a-2 goes back to 3000m, and with c-1 raised
        from 500m to 1300m m2 would hold 4300m, so c is held too. x has no reading and keeps its 3000m.
        """
        pods = [
            placed(api_pod("a-1", 500), "m1", workload=A),
            placed(api_pod("x-1", 3000), "m1", workload=X),
            placed(api_pod("a-2", 3000), "m2", workload=A),
            placed(api_pod("c-1", 500), "m2", workload=C),
        ]
        found = {**peak("a-1", "app", 1000, 64 * MEBIBYTE), **peak("a-2", "app", 100, 64 * MEBIBYTE)}
        found.update(peak("c-1", "app", 1000, 64 * MEBIBYTE))
        after = SmallerPods(found, ()).apply(state([pool()], [machine("m1"), machine("m2")], pods))
        self.assertEqual(
            {pod.source.name: (pod.request["cpu"], pod.held) for pod in after.pods},
            {"a-1": (500, True), "x-1": (3000, False), "a-2": (3000, True), "c-1": (500, True)},
        )
        self.assertFalse(any(pod.raised for pod in after.pods))

    def test_a_daemonset_whose_raise_overfills_a_machine_is_held(self) -> None:
        """Each machine gives the DaemonSet's pod its request first: 2600m beside web's 3000m is more than 4000m."""
        daemon = Workload("DaemonSet", "shop", "logs")
        logs = placed(api_pod("logs-m1", 100, controller=("DaemonSet", "logs")), "m1", workload=daemon)
        web = placed(api_pod("web-1", 3000), "m1", workload=WEB)
        after = resized([logs, web], peak("logs-m1", "app", 2000, 64 * MEBIBYTE))
        self.assertEqual(after.pods, (replace(logs, held=True), web))


class LimitRangeTest(unittest.TestCase):
    """The namespace's LimitRanges bound each new request, and a pod that would break one of type Pod keeps its own."""

    def test_container_min_max_and_ratio_hold(self) -> None:
        """A 50m peak asks 65m but the min is 200m; 400 MiB asks 520 MiB but the max is 256 MiB.

        With a 2000m limit and a ratio of 4, the request is at least 500m.
        """
        item = LimitItem("Container", {"cpu": 200}, {"memory": 256 * MEBIBYTE}, {"cpu": Fraction(4)})
        ranges = [LimitRange("shop", (item,))]
        plain = Container("app", {"cpu": 1000, "memory": 1024 * MEBIBYTE}, {}, ())
        after = resized([with_containers("web-1", plain)], peak("web-1", "app", 50, 400 * MEBIBYTE), ranges)
        self.assertEqual((requests(after)["web-1"]["cpu"], requests(after)["web-1"]["memory"]), (200, 256 * MEBIBYTE))
        capped = Container("app", {"cpu": 1000, "memory": 64 * MEBIBYTE}, {"cpu": 2000}, ())
        after = resized([with_containers("web-1", capped)], peak("web-1", "app", 100, 40 * MEBIBYTE), ranges)
        self.assertEqual(requests(after)["web-1"]["cpu"], 500)

    def test_a_pod_limit_range_the_new_total_would_break_keeps_todays_requests(self) -> None:
        """The pod's new total, 130m, is below the Pod LimitRange's 500m min, so it keeps today's 1000m."""
        ranges = [LimitRange("shop", (LimitItem("Pod", {"cpu": 500}, {}, {}),))]
        pods = [placed(api_pod("web-1", 1000), "m1", workload=WEB)]
        after = resized(pods, peak("web-1", "app", 100, 50 * MEBIBYTE), ranges)
        self.assertEqual(after.pods, tuple(pods))


class PodShapesTest(unittest.TestCase):
    """Pod-level requests, sidecars and init containers, static pods and pods the platform puts back."""

    def test_a_pod_level_request_follows_its_containers_summed_peaks(self) -> None:
        """Peaks of 300m and 200m sum to 500m, so the pod asks 650m; 500 and 300 MiB sum to 800, so 1040 MiB."""
        app = Container("app", {"cpu": 1000, "memory": 1024 * MEBIBYTE}, {}, ())
        proxy = Container("proxy", {"cpu": 500, "memory": 512 * MEBIBYTE}, {}, ())
        pod = with_containers("web-1", app, proxy, requests={"cpu": 2000, "memory": 2048 * MEBIBYTE})
        found = {**peak("web-1", "app", 300, 500 * MEBIBYTE), **peak("web-1", "proxy", 200, 300 * MEBIBYTE)}
        after = resized([pod], found)
        self.assertEqual(after.pods[0].request, {"cpu": 650, "memory": 1040 * MEBIBYTE, "pods": 1})

    def test_sidecars_are_sized_and_other_init_containers_keep_their_requests(self) -> None:
        """The sidecar runs beside app, so both are sized; setup starts alone at 2000m, which stays the pod's CPU."""
        app = Container("app", {"cpu": 1000, "memory": 512 * MEBIBYTE}, {}, ())
        setup = InitContainer("setup", {"cpu": 2000}, {}, False)
        shipper = InitContainer("shipper", {"cpu": 500, "memory": 256 * MEBIBYTE}, {}, True)
        pod = with_containers("web-1", app, init_containers=(setup, shipper))
        found = {**peak("web-1", "app", 100, 100 * MEBIBYTE), **peak("web-1", "shipper", 100, 100 * MEBIBYTE)}
        after = resized([pod], found)
        self.assertEqual(after.pods[0].request, {"cpu": 2000, "memory": 260 * MEBIBYTE, "pods": 1})

    def test_static_and_reconciled_pods_keep_their_requests(self) -> None:
        """The API refuses changes to a static pod's mirror, and the platform puts back a pod it reconciles."""
        static = placed(api_pod("proxy-m1", 500, controller=("Node", "m1")), "m1")
        reconciled = placed(api_pod("dns-1", 500, labels={ADDON_MODE: "Reconcile"}), "m1")
        after = resized([static, reconciled], peaks([static, reconciled], 100, 50 * MEBIBYTE))
        self.assertEqual(after.pods, (static, reconciled))
        self.assertEqual(static.source.controller, OwnerRef("Node", "m1"))

    def test_smaller_pods_alone_saves_nothing(self) -> None:
        """The machines don't change, so the step's cost after is its cost before."""
        pods = [placed(api_pod(f"web-{n}", 1000), "m1", workload=WEB) for n in range(1, 4)]
        today = state([pool()], [machine("m1")], pods)
        after, steps = Pipeline([SmallerPods(peaks(pods, 100, 50 * MEBIBYTE), ())]).run(today)
        self.assertEqual([(step.before, step.after, step.saving) for step in steps], [(146, 146, 0)])
        self.assertEqual(after.machines, today.machines)
