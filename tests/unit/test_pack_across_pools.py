"""Step 4, packing across pools: the figures worked out by hand, the moves the checker blocks, and the pools' limits."""

import unittest
from collections import Counter
from dataclasses import replace
from typing import Any, Dict

from tests.unit.fleet import GIB, TYPE, api_pod, machine, placed, pool, state, taint
from tests.unit.test_cheaper_types import D8S, offer, search_of
from zoorik.kubectl.objects import NodeSelectorTerm, PersistentVolume, Requirement, Toleration
from zoorik.kubernetes.assessment import line_results
from zoorik.kubernetes.collect import NODE_GROUP, TYPE_LABEL
from zoorik.kubernetes.model import ClusterState, Line, Machine, Pod, Pool
from zoorik.kubernetes.placement import ARCH_LABEL, ZONE_LABEL
from zoorik.kubernetes.steps import SHARED_ROW, Pipeline, cost
from zoorik.kubernetes.steps.cheaper_types import CheaperTypes
from zoorik.kubernetes.steps.pack_across_pools import PackAcrossPools, across
from zoorik.kubernetes.steps.smaller_pods import SmallerPods
from zoorik.kubernetes.steps.spare_machines import SpareMachines
from zoorik.pricing.machines import Capacity

# The worked figures' machine types: A and D give 4 cores and 16 GiB, B 8 cores and 32 GiB.
TYPE_A = "a.xlarge"
TYPE_D = "d.xlarge"
TYPE_B = "b.2xlarge"
TYPE_E = "Standard_E4s_v5"
CHEAP = pool("p", 0.1)
DEAR = pool("q", 0.2)


def where(after: ClusterState) -> Dict[str, str]:
    """Give each pod's machine, by pod name."""
    return {pod.source.name: pod.node for pod in after.pods}


def machines_of(after: ClusterState) -> Dict[str, int]:
    """Count each pool's machines, by pool id."""
    return dict(Counter(each.pool for each in after.machines))


def two_pools_of_d() -> ClusterState:
    """Make pools p and q of D at $0.18 an hour: p's five pods of 1 core and 2 GiB on two machines, q's three on one."""
    p, q = pool("p", 0.18), pool("q", 0.18)
    layout = {"p1": (p, 3), "p2": (p, 2), "q1": (q, 3)}
    pods = [
        placed(api_pod(f"{name}-{index}", 1000, 2 * GIB), name, each.id)
        for name, (each, number) in layout.items()
        for index in range(number)
    ]
    return state([p, q], [machine(name, pool=each.id) for name, (each, _) in layout.items()], pods)


def groups_on_a() -> ClusterState:
    """Make node groups p and q of A at $0.20 an hour, one pod of 1 core and 2 GiB on each machine: five and three."""
    groups = [
        Pool(f"{NODE_GROUP} {name}", name, NODE_GROUP, False, (Line(TYPE_A, Capacity.REGULAR, 0.2, ""),))
        for name in "pq"
    ]
    machines = []
    pods = []
    for group, number in zip(groups, (5, 3)):
        for index in range(1, number + 1):
            name = f"{group.name}{index}"
            machines.append(machine(name, pool=group.id, machine_type=TYPE_A))
            pods.append(placed(api_pod(f"{name}-pod", 1000, 2 * GIB), name, group.id))
    return state(groups, machines, pods)


def on(name: str, node: Machine, source_changes: Dict[str, Any], **changes: Any) -> Pod:
    """Place a pod of 1 core and 1 GiB called name on node, its pod spec and placement changed."""
    return placed(api_pod(name, 1000, GIB, **source_changes), node.name, node.pool, **changes)


class WorkedFigureTest(unittest.TestCase):
    """The figures the plan works out by hand."""

    def test_two_pools_of_d_go_from_394_to_263(self) -> None:
        """The two machines of p hold all eight pods, four each, so q's machine goes: $263 + $131 = $394 to $263."""
        before = two_pools_of_d()
        after, results = Pipeline([PackAcrossPools()]).run(before)
        figures = [(each.name, each.row, each.before, each.after, each.saving) for each in results]
        self.assertEqual(figures, [("Packing across pools", SHARED_ROW, 394, 263, 131)])
        self.assertEqual([each.name for each in after.machines], ["p1", "p2"])
        self.assertEqual({pod.pool for pod in after.pods}, {"node pool p"})

    def test_all_steps_through_step_4_go_from_1168_to_263(self) -> None:
        """Step 1 saves $0, step 2 $730 (p keeps two machines, q one), step 3 $44 (both on D), and step 4 $131."""
        today = groups_on_a()
        offers = [offer(TYPE_A, 0.2, 4, 16), offer(TYPE_D, 0.18, 4, 16), offer(TYPE_B, 0.38, 8, 32)]
        cheaper = CheaperTypes(search_of(today, offers))
        after, results = Pipeline([SmallerPods({}, ()), SpareMachines(), cheaper, PackAcrossPools()]).run(today)
        figures = [(each.name, each.before, each.after, each.saving) for each in results]
        self.assertEqual(
            figures,
            [
                ("Smaller pods", 1168, 1168, 0),
                ("Spare machines", 1168, 438, 730),
                ("Cheaper machine types", 438, 394, 44),
                ("Packing across pools", 394, 263, 131),
            ],
        )
        self.assertEqual([(each.pool, each.machine_type) for each in after.machines], [(f"{NODE_GROUP} p", TYPE_D)] * 2)
        self.assertEqual(sum(each.saving for each in results), cost(today) - cost(after))


class BlockedMoveTest(unittest.TestCase):
    """Moves the checker blocks: the pod stays in its pool, and so does its machine."""

    def test_with_nothing_in_the_way_the_pod_joins_the_cheaper_machine(self) -> None:
        """q-1 fits beside p-1, so q's machine goes: $219 a month to $73."""
        p1, q1 = machine("p1", pool=CHEAP.id), machine("q1", pool=DEAR.id)
        before = state([CHEAP, DEAR], [p1, q1], [on("p-1", p1, {}), on("q-1", q1, {})])
        after = PackAcrossPools().apply(before)
        self.assertEqual((cost(before), cost(after)), (219, 73))
        self.assertEqual(where(after), {"p-1": "p1", "q-1": "p1"})

    def test_a_selector_a_taint_a_zone_bound_volume_and_another_architecture_each_keep_the_pod(self) -> None:
        """Each of four rules keeps q-1 and q's machine where they are.

        q-1 selects a label only q's machine has; p's machine has a taint q-1 doesn't tolerate; q-1's volume is bound to
        zone 2, where p has no machine; or p runs arm64 and q-1 amd64.
        """
        zone_2 = NodeSelectorTerm((Requirement(ZONE_LABEL, "In", ("zone-2",)),), ())
        volume = PersistentVolume("pv-q", "disk.csi.azure.com", None, (zone_2,), False)
        dedicated = taint("dedicated", value="p")
        tolerates = (Toleration("dedicated", "Equal", "p", "NoSchedule"),)
        p1, q1 = machine("p1", pool=CHEAP.id), machine("q1", pool=DEAR.id)
        team, tainted = replace(q1, labels={**q1.labels, "team": "q"}), replace(p1, taints=(dedicated,))
        zoned, arm = machine("q1", "zone-2", DEAR.id), machine("p1", pool=CHEAP.id, labels={ARCH_LABEL: "arm64"})
        cases = {
            "selector": (p1, team, on("p-1", p1, {}), on("q-1", team, {"node_selector": {"team": "q"}})),
            "taint": (tainted, q1, on("p-1", tainted, {"tolerations": tolerates}), on("q-1", q1, {})),
            "zone-bound volume": (p1, zoned, on("p-1", p1, {}), on("q-1", zoned, {}, volumes=(volume,))),
            "architecture": (arm, q1, on("p-1", arm, {}, arch="arm64"), on("q-1", q1, {})),
        }
        for name, (p_machine, q_machine, p_pod, q_pod) in cases.items():
            with self.subTest(name):
                before = state([CHEAP, DEAR], [p_machine, q_machine], [p_pod, q_pod])
                self.assertEqual(PackAcrossPools().apply(before), before)

    def test_a_pod_that_selects_a_type_label_moves_only_to_that_type(self) -> None:
        """q-1 selects D: it moves to r's D machine at $0.15, not to p's E at $0.10, and q's machine goes."""
        p = Pool(CHEAP.id, "p", "node pool", False, (Line(TYPE_E, Capacity.REGULAR, 0.1, ""),))
        r = pool("r", 0.15)
        p1 = machine("p1", pool=p.id, labels={TYPE_LABEL: TYPE_E}, machine_type=TYPE_E)
        q1, r1 = (machine(name, pool=each.id, labels={TYPE_LABEL: TYPE}) for name, each in (("q1", DEAR), ("r1", r)))
        pods = [on("p-1", p1, {}), on("q-1", q1, {"node_selector": {TYPE_LABEL: TYPE}}), on("r-1", r1, {})]
        before = state([p, DEAR, r], [p1, q1, r1], pods)
        after = PackAcrossPools().apply(before)
        self.assertEqual((cost(before), cost(after)), (329, 183))
        self.assertEqual(where(after), {"p-1": "p1", "q-1": "r1", "r-1": "p1"})


def spot_and_regular(capacity: Capacity) -> ClusterState:
    """Make two pools: spot, one machine at $0.04 bought as capacity, and regular, two machines at $0.20.

    The spot machine holds a pod of 1 core, and each regular machine two pods of 1.5 cores.
    """
    spot = Pool("node pool spot", "spot", "node pool", False, (Line(TYPE, capacity, 0.04, ""),))
    regular = pool("regular")
    s1 = machine("s1", pool=spot.id, capacity=capacity)
    pods = [placed(api_pod("s-1", 1000, GIB), "s1", spot.id)]
    pods += [placed(api_pod(f"r-{index}", 1500, GIB), f"r{1 + index // 2}", regular.id) for index in range(4)]
    return state([spot, regular], [s1, machine("r1", pool=regular.id), machine("r2", pool=regular.id)], pods)


class PoolLimitTest(unittest.TestCase):
    """The platform minimum, spot pools, unpriced pools, and a packing that would cost more."""

    def test_a_system_pool_keeps_one_machine_and_an_emptied_pool_shows_0(self) -> None:
        """Every pod fits the cheap pool's machine: the System pool keeps one of its two machines, and user has none."""
        system, user, cheap = pool("system", system=True), pool("user"), pool("cheap", 0.1)
        layout = {"s1": system, "s2": system, "u1": user, "c1": cheap}
        before = state(
            [system, user, cheap],
            [machine(name, pool=each.id) for name, each in layout.items()],
            [placed(api_pod(f"{name}-pod", 1000, GIB), name, each.id) for name, each in layout.items()],
        )
        after = PackAcrossPools().apply(before)
        self.assertEqual(([each.name for each in after.machines], set(where(after).values())), (["c1", "s1"], {"c1"}))
        lines = [(each.pool, each.today, each.after) for each in line_results(before, after)]
        self.assertEqual(lines, [("system", 2, 1), ("user", 1, 0), ("cheap", 1, 1)])

    def test_a_pool_on_spot_today_grows_by_no_machine(self) -> None:
        """The spot machine takes two of regular's pods into its room, and one regular machine stays: $321 to $175.

        Bought regular at the same price, the pool would take a second machine and every pod: $321 to $58.
        """
        on_spot, bought_regular = spot_and_regular(Capacity.SPOT), spot_and_regular(Capacity.REGULAR)
        after_spot, after_regular = PackAcrossPools().apply(on_spot), PackAcrossPools().apply(bought_regular)
        self.assertEqual((cost(on_spot), cost(after_spot)), (321, 175))
        self.assertEqual(machines_of(after_spot), {"node pool spot": 1, "node pool regular": 1})
        self.assertEqual((cost(bought_regular), cost(after_regular)), (321, 58))
        self.assertEqual(machines_of(after_regular), {"node pool spot": 2})

    def test_a_pool_with_no_price_takes_no_part(self) -> None:
        """The unpriced pool's machine has room, but takes neither of the priced pool's pods, and keeps its own."""
        unpriced, priced = pool("unpriced", None, note="no list price"), pool("priced")
        u1, p1, p2 = machine("u1", pool=unpriced.id), machine("p1", pool=priced.id), machine("p2", pool=priced.id)
        before = state([unpriced, priced], [u1, p1, p2], [on("u-1", u1, {}), on("p-1", p1, {}), on("p-2", p2, {})])
        after = PackAcrossPools().apply(before)
        self.assertEqual((cost(before), cost(after)), (292, 146))
        self.assertEqual(
            (where(after), [each.name for each in after.machines]),
            ({"u-1": "u1", "p-1": "p1", "p-2": "p1"}, ["p1", "u1"]),
        )

    def test_a_packing_that_would_cost_more_is_not_applied(self) -> None:
        """Six pods of 2.5 cores would need six of small's machines, $438, more than today's $394: it stays as it is.

        Small's machines cost less per share of the pods' demand than large's, but each holds only one of the pods.
        """
        small = pool("small", 0.1)
        large = Pool("node pool large", "large", "node pool", False, (Line(D8S, Capacity.REGULAR, 0.22, ""),))
        room = {"cpu": 8000, "memory": 32 * GIB, "pods": 110}
        larges = [machine(name, pool=large.id, machine_type=D8S, allocatable=room) for name in ("l1", "l2")]
        pods = [placed(api_pod(f"l-{index}", 2500, GIB), f"l{1 + index // 3}", large.id) for index in range(6)]
        before = state([small, large], [machine("s1", pool=small.id), *larges], pods)
        packed = across(before, grow=True)
        if packed is None:
            raise AssertionError("the pods found no packing")
        self.assertEqual((cost(before), cost(packed), machines_of(packed)), (394, 438, {small.id: 6}))
        after, results = Pipeline([PackAcrossPools()]).run(before)
        self.assertEqual((after, [(each.before, each.after) for each in results]), (before, [(394, 394)]))

    def test_a_line_a_step_added_and_a_later_step_emptied_is_left_out(self) -> None:
        """Step 3 gave apps an E line that step 4 emptied: with no machine today or after, the result leaves it out."""
        today = state([pool()], [machine("m1")], [])
        lines = (*today.pools[0].lines, Line(TYPE_E, Capacity.REGULAR, 0.1, ""))
        after = replace(today, pools=(replace(today.pools[0], lines=lines),), machines=())
        self.assertEqual(
            [(each.machine_type, each.today, each.after) for each in line_results(today, after)], [(TYPE, 1, 0)]
        )
