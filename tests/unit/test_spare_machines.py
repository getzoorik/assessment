"""Step 2, spare machines, and the pipeline both steps run in: the figures worked out by hand."""

import unittest
from dataclasses import replace
from typing import Any, Dict, List

from tests.unit.fleet import GIB, api_pod, local_volume, machine, peaks, placed, pool, state
from zoorik.kubectl.objects import (
    AffinityTerm,
    LabelSelector,
    PersistentVolume,
    SpreadConstraint,
    with_placement_labels,
)
from zoorik.kubectl.usage import MEBIBYTE
from zoorik.kubernetes.collect import LOCAL_VOLUME, NAMESPACES_BY_LABEL, NO_CONTROLLER
from zoorik.kubernetes.model import ClusterState, Pod, StepResult, Workload
from zoorik.kubernetes.placement import HOSTNAME, OS_LABEL
from zoorik.kubernetes.shares import workload_results
from zoorik.kubernetes.steps import Pipeline, cost
from zoorik.kubernetes.steps.smaller_pods import SmallerPods
from zoorik.kubernetes.steps.spare_machines import SpareMachines

A = Workload("Deployment", "shop", "a")
B = Workload("Deployment", "shop", "b")


def names(state_after: ClusterState) -> List[str]:
    """Name the machines a state keeps."""
    return [each.name for each in state_after.machines]


def where(state_after: ClusterState) -> Dict[str, str]:
    """Give each pod's machine, by pod name."""
    return {pod.source.name: pod.node for pod in state_after.pods}


def worked_figure() -> ClusterState:
    """Five machines at $0.20 an hour; A's eight pods ask 1160m and 1812 MiB each, B's four 1160m and 4372 MiB.

    Today three pods fit a machine: A1-A3, A4-A6, A7 A8 B1, B2 B3, and B4.
    """
    layout = {
        "m1": ["a-1", "a-2", "a-3"],
        "m2": ["a-4", "a-5", "a-6"],
        "m3": ["a-7", "a-8", "b-1"],
        "m4": ["b-2", "b-3"],
        "m5": ["b-4"],
    }
    pods = [
        placed(
            api_pod(name, 1160, (1812 if name.startswith("a") else 4372) * MEBIBYTE),
            node,
            workload=A if name.startswith("a") else B,
        )
        for node, pod_names in layout.items()
        for name in pod_names
    ]
    return state([pool()], [machine(node) for node in layout], pods)


def spread(key: str, app: str = "web") -> SpreadConstraint:
    """Make a DoNotSchedule spread constraint of maxSkew 1 over key, for the pods labelled app."""
    return SpreadConstraint(1, key, "DoNotSchedule", LabelSelector({"app": app}, ()), None, None, None, ())


def web(name: str, node: str, **changes: Any) -> Pod:
    """Place a replica of web, labelled app=web, with a spread constraint over machines."""
    source = api_pod(name, labels={"app": "web"}, spread=(spread(HOSTNAME),), **changes)
    return placed(source, node, workload=Workload("Deployment", "shop", "web"))


class WorkedFigureTest(unittest.TestCase):
    """The figure the plan works out by hand."""

    def test_smaller_pods_then_spare_machines_save_292_split_134_and_158(self) -> None:
        """Every pod peaks at 700m and 1000 MiB, so asks 910m and 1300 MiB: four fit a machine, and three do.

        $730 a month goes to $438. A frees 2000m and 4096 MiB, B 1000m and 12288 MiB: A's weight is half of 2/3 plus
        half of 1/4, so $133.83 of $292, and B's $158.17; the dollar left goes to A's larger remainder.
        """
        today = worked_figure()
        after, steps = Pipeline([SmallerPods(peaks(today.pods, 700, 1000 * MEBIBYTE), ()), SpareMachines()]).run(today)
        figures = [(step.name, step.before, step.after, step.saving) for step in steps]
        self.assertEqual(figures, [("Smaller pods", 730, 730, 0), ("Spare machines", 730, 438, 292)])
        self.assertEqual(names(after), ["m1", "m2", "m3"])
        shares = {
            (result.name, result.cpu, result.memory, result.share) for result in workload_results(today, after, 292)
        }
        self.assertEqual(shares, {("a", 2000, 4096 * MEBIBYTE, 134), ("b", 1000, 12288 * MEBIBYTE, 158)})


class FitAndPinningTest(unittest.TestCase):
    """What keeps a machine: resources a pod needs, attach limits, and pods that stay on their node."""

    def test_two_one_gpu_pods_on_two_one_gpu_machines_keep_both(self) -> None:
        """Each machine holds one GPU, so the two pods can't share one, however little else they ask."""
        gpu = {"cpu": 4000, "memory": 16 * GIB, "pods": 110, "nvidia.com/gpu": 1}
        machines = [machine(name, allocatable=gpu) for name in ("m1", "m2")]
        pods = []
        for name, node in (("train-1", "m1"), ("train-2", "m2")):
            source = api_pod(name)
            container = replace(
                source.containers[0], requests={"cpu": 100, "memory": 128 * MEBIBYTE, "nvidia.com/gpu": 1}
            )
            pods.append(placed(replace(source, containers=(container,)), node))
        today = state([pool()], machines, pods)
        self.assertEqual(SpareMachines().apply(today), today)

    def test_an_attach_limit_keeps_a_machine(self) -> None:
        """Each machine attaches one disk, so the two pods with a disk each stay on two machines."""
        machines = [machine(name, attach={"disk.csi.azure.com": 1}) for name in ("m1", "m2")]
        pods = [
            placed(api_pod(name), node, volumes=(PersistentVolume(f"pv-{name}", "disk.csi.azure.com", None, None),))
            for name, node in (("db-1", "m1"), ("db-2", "m2"))
        ]
        today = state([pool()], machines, pods)
        self.assertEqual(SpareMachines().apply(today), today)
        without_limit = state([pool()], [replace(each, attach={}) for each in machines], pods)
        self.assertEqual(names(SpareMachines().apply(without_limit)), ["m1"])

    def test_a_pod_with_no_controller_keeps_its_node(self) -> None:
        """Nothing would recreate the bare pod elsewhere, so its machine stays and the other pod joins it."""
        pods = [placed(api_pod("app-1"), "m1"), placed(api_pod("debug", controller=None), "m2", stays=NO_CONTROLLER)]
        after = SpareMachines().apply(state([pool()], [machine("m1"), machine("m2")], pods))
        self.assertEqual((names(after), where(after)), (["m2"], {"app-1": "m2", "debug": "m2"}))

    def test_a_local_volume_keeps_its_node(self) -> None:
        """A pod whose volume is bound to its node keeps that machine, and the other pod joins it."""
        volume = local_volume("pv-local", "m2")
        pods = [placed(api_pod("app-1"), "m1"), placed(api_pod("db-0"), "m2", volumes=(volume,), stays=LOCAL_VOLUME)]
        after = SpareMachines().apply(state([pool()], [machine("m1"), machine("m2")], pods))
        self.assertEqual((names(after), where(after)), (["m2"], {"app-1": "m2", "db-0": "m2"}))


class SpreadAndAffinityTest(unittest.TestCase):
    """Spread and pod affinity, counted as the scheduler counts them."""

    def test_three_replicas_with_hostname_spread_end_on_three_machines(self) -> None:
        """One machine would hold all four pods, but spread over machines keeps the replicas apart: m4 goes."""
        pods = [web("web-1", "m1"), web("web-2", "m2"), web("web-3", "m3"), placed(api_pod("app-1"), "m4")]
        after = SpareMachines().apply(state([pool()], [machine(f"m{n}") for n in range(1, 5)], pods))
        self.assertEqual(names(after), ["m1", "m2", "m3"])
        self.assertEqual(len({where(after)[name] for name in ("web-1", "web-2", "web-3")}), 3)

    def test_zone_spread_counts_an_empty_zone(self) -> None:
        """Two replicas spread over zones keep a machine in each zone: a third zone with no pod counts as 0."""
        zone_spread = (spread("topology.kubernetes.io/zone"),)
        pods = [
            placed(api_pod(name, labels={"app": "web"}, spread=zone_spread), node)
            for name, node in (("web-1", "m1"), ("web-2", "m2"))
        ]
        machines = [machine("m1", "zone-1"), machine("m2", "zone-2"), machine("m3", "zone-3")]
        after = SpareMachines().apply(state([pool()], machines, pods))
        self.assertEqual(len({after_machine.zone for after_machine in after.machines}), 2)
        self.assertEqual(len(after.machines), 2)

    def test_required_affinity_places_a_pod_after_its_target(self) -> None:
        """The larger pod needs the cache's machine, so it is placed after the cache, and lands beside it."""
        near = AffinityTerm(LabelSelector({"app": "cache"}, ()), (), None, HOSTNAME, (), ())
        pods = [
            placed(api_pod("cache-1", 100, labels={"app": "cache"}), "m2"),
            placed(api_pod("app-1", 2000, affinity=(near,)), "m1"),
        ]
        after = SpareMachines().apply(state([pool()], [machine("m1"), machine("m2")], pods))
        self.assertEqual(len(after.machines), 1)
        self.assertEqual(where(after)["app-1"], where(after)["cache-1"])

    def test_a_placed_pods_anti_affinity_blocks_a_move_onto_its_machine(self) -> None:
        """The guard's anti-affinity rules out web's machine, so the two pods keep two machines."""
        away = AffinityTerm(LabelSelector({"app": "web"}, ()), (), None, HOSTNAME, (), ())
        pods = [
            placed(api_pod("guard", 2000, anti_affinity=(away,)), "m1"),
            placed(api_pod("web-1", labels={"app": "web"}), "m2"),
        ]
        today = state([pool()], [machine("m1"), machine("m2")], pods)
        self.assertEqual(SpareMachines().apply(today), today)
        allowed = [replace(pods[0], source=replace(pods[0].source, anti_affinity=())), pods[1]]
        self.assertEqual(len(SpareMachines().apply(replace(today, pods=tuple(allowed))).machines), 1)

    def test_a_term_that_selects_namespaces_by_label_keeps_its_pod(self) -> None:
        """The run reads no namespace labels, so the pod whose term picks namespaces by label keeps its machine."""
        beside = AffinityTerm(
            LabelSelector({"app": "cache"}, ()), (), LabelSelector({"team": "one"}, ()), HOSTNAME, (), ()
        )
        pods = [
            placed(api_pod("web-1"), "m1"),
            placed(api_pod("app-1", affinity=(beside,)), "m2", stays=NAMESPACES_BY_LABEL),
        ]
        after = SpareMachines().apply(state([pool()], [machine("m1"), machine("m2")], pods))
        self.assertEqual((names(after), where(after)), (["m2"], {"web-1": "m2", "app-1": "m2"}))

    def test_placement_is_the_same_with_unused_labels_dropped(self) -> None:
        """Labels no placement term reads change nothing: the packing is the same with them dropped."""
        labels = {"app": "web", "team": "one", "version": "v2", "pod-template-hash": "5d8f"}
        pods = [
            placed(api_pod(f"web-{n}", 1500, labels=labels, spread=(spread(HOSTNAME),)), f"m{n}") for n in range(1, 4)
        ]
        pods.append(placed(api_pod("app-1", 300, labels={"app": "app", "tier": "front"}), "m4"))
        machines = [machine(f"m{n}") for n in range(1, 5)]
        narrowed = with_placement_labels([pod.source for pod in pods])
        dropped = [replace(pod, source=source) for pod, source in zip(pods, narrowed)]
        self.assertEqual(narrowed[0].labels, {"app": "web"})
        full = SpareMachines().apply(state([pool()], machines, pods))
        lean = SpareMachines().apply(state([pool()], machines, dropped))
        self.assertEqual((names(full), where(full)), (names(lean), where(lean)))


class PoolsTest(unittest.TestCase):
    """Unpriced pools and the platform minimum."""

    def test_a_windows_pool_takes_no_pods_and_adds_nothing_to_cost_today(self) -> None:
        """The Windows pool has no price, so it counts in no total, keeps its machines and takes no Linux pod."""
        windows = pool("win", None, note="only Linux machines are priced")
        win_machines = [machine(name, pool=windows.id, labels={OS_LABEL: "windows"}) for name in ("w1", "w2")]
        pods = [placed(api_pod("app-1"), "m1"), placed(api_pod("app-2"), "m2")]
        today = state([pool(), windows], [machine("m1"), machine("m2"), *win_machines], pods)
        after = SpareMachines().apply(today)
        self.assertEqual((cost(today), cost(after)), (292, 146))
        self.assertEqual(names(after), ["m1", "w1", "w2"])
        self.assertEqual(set(where(after).values()), {"m1"})

    def test_an_aks_system_pool_keeps_one_machine_and_a_user_pool_reaches_none(self) -> None:
        """Pools with only machine overhead on them: the system pool keeps one machine, the user pool none."""
        system, user = pool("system", system=True), pool("user")
        daemons = [
            placed(api_pod(f"agent-{node}", controller=("DaemonSet", "agent")), node, pool=pool_id)
            for node, pool_id in (("s1", system.id), ("s2", system.id), ("u1", user.id), ("u2", user.id))
        ]
        machines = [
            machine("s1", pool=system.id),
            machine("s2", pool=system.id),
            machine("u1", pool=user.id),
            machine("u2", pool=user.id),
        ]
        after = SpareMachines().apply(state([system, user], machines, daemons))
        self.assertEqual(names(after), ["s1"])


class StepThatAddsMachines:
    """A step that adds a machine to the first pool, so it would cost more."""

    name = "Adds a machine"
    row = "test"

    def apply(self, before: ClusterState) -> ClusterState:
        """Add one more machine like the first."""
        extra = replace(before.machines[0], name="extra")
        return replace(before, machines=(*before.machines, extra))


class PipelineTest(unittest.TestCase):
    """The steps run in order, each on what the one before left."""

    def test_a_step_that_would_cost_more_is_not_applied(self) -> None:
        """A step whose state costs more than the one before it leaves the state as it was, and saves $0."""
        today = worked_figure()
        after, steps = Pipeline([StepThatAddsMachines()]).run(today)
        self.assertEqual((after, steps), (today, (StepResult("Adds a machine", "test", 730, 730),)))

    def test_a_workload_whose_raise_a_machine_has_no_room_for_is_packed_at_todays_requests(self) -> None:
        """Every pod peaks at 3000m, so would ask 3900m: three on m1 ask 11,700m of its 4000m, so all keep 100m.

        At today's requests the four pods fit m1, so m2 goes: $146 a month.
        """
        pods = [placed(api_pod(f"app-{n}"), "m1") for n in range(1, 4)] + [placed(api_pod("app-4"), "m2")]
        today = state([pool()], [machine("m1"), machine("m2")], pods)
        after, steps = Pipeline([SmallerPods(peaks(today.pods, 3000, 100 * MEBIBYTE), ()), SpareMachines()]).run(today)
        self.assertEqual(names(after), ["m1"])
        self.assertEqual([step.saving for step in steps], [0, 146])
        self.assertEqual([pod.request for pod in after.pods], [pod.request for pod in today.pods])
        self.assertTrue(all(pod.held and not pod.raised for pod in after.pods))

    def test_the_steps_add_up_and_none_is_negative(self) -> None:
        """Each step starts where the one before ended, so the savings add up to today's cost less the cost after."""
        today = worked_figure()
        after, steps = Pipeline(
            [SmallerPods(peaks(today.pods, 700, 1000 * MEBIBYTE), ()), StepThatAddsMachines(), SpareMachines()]
        ).run(today)
        self.assertEqual(steps[0].before, cost(today))
        self.assertEqual([step.before for step in steps[1:]], [step.after for step in steps[:-1]])
        self.assertEqual(sum(step.saving for step in steps), cost(today) - cost(after))
        self.assertTrue(all(step.saving >= 0 for step in steps))
