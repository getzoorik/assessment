"""Step 6, spot: the figures worked out by hand, the lines that stay regular, and all six steps in order."""

import unittest
from dataclasses import replace
from typing import List

from tests.unit.fleet import GIB, TYPE, peaks, pool, state
from tests.unit.test_cheaper_types import offer, search_of
from tests.unit.test_fewer_copies import NOTHING, WEB, app
from tests.unit.test_pack_across_pools import TYPE_A, TYPE_B, TYPE_D, groups_on_a
from zoorik.kubernetes.assessment import line_results
from zoorik.kubernetes.model import ClusterState, Line, Pool
from zoorik.kubernetes.placement import empty_room, overheads
from zoorik.kubernetes.steps import Pipeline, Step, cost, fewer_copies
from zoorik.kubernetes.steps.cheaper_types import CheaperTypes
from zoorik.kubernetes.steps.fewer_copies import FewerCopies
from zoorik.kubernetes.steps.pack_across_pools import PackAcrossPools
from zoorik.kubernetes.steps.smaller_pods import SmallerPods
from zoorik.kubernetes.steps.spare_machines import SpareMachines
from zoorik.kubernetes.steps.spot import ASSUMPTION, ROW, Spot
from zoorik.pricing.machines import Capacity

NO_SPOT_PRICE = f"no spot price for {TYPE} in westeurope"


def over_full(after: ClusterState) -> List[str]:
    """Name the machines, busy or at quiet times, whose pods ask for more than they have after their pool's overhead."""
    overhead = overheads(after)
    fleets = [(after.machines, after.pods)]
    if after.quiet is not None:
        fleets.append((after.quiet.machines, after.quiet.pods))
    names = []
    for machines, pods in fleets:
        for machine in machines:
            room = empty_room(machine, overhead[machine.pool])
            for pod in pods:
                if pod.node == machine.name and not pod.overhead:
                    room.take(pod)
            if any(free < 0 for free in room.free.values()):
                names.append(machine.name)
    return names


class WorkedFigureTest(unittest.TestCase):
    """The figures worked out by hand."""

    def test_the_machine_left_at_quiet_times_runs_spot_then(self) -> None:
        """After step 5's $219, the one machine of the quiet hours at $0.08: 730 x (0.5 x 2 x 0.2 + 0.5 x 1 x 0.08).

        That is $175, a saving of $44.
        """
        today = app(4)
        after, results = Pipeline([FewerCopies(NOTHING), Spot({TYPE: 0.08})]).run(today)
        figures = [(each.row, each.assumption, each.before, each.after) for each in results]
        self.assertEqual(figures, [(fewer_copies.ROW, fewer_copies.ASSUMPTION, 292, 219), (ROW, ASSUMPTION, 219, 175)])
        line = line_results(today, after)[0]
        self.assertEqual((line.today, line.after, line.quiet, line.spot, line.cost_after), (2, 2, 1, 0.08, 175))
        self.assertEqual(ASSUMPTION, "the machines running at quiet times, 12 h a day, on spot")

    def test_without_fewer_copies_the_same_machines_run_spot_at_quiet_times(self) -> None:
        """With no app to drop, both machines run spot 12 hours a day: 730 x (0.5 x 2 x 0.2 + 0.5 x 2 x 0.08), $204."""
        today = app(2, per_machine=1)
        after = Spot({TYPE: 0.08}).apply(today)
        self.assertEqual((cost(today), cost(after)), (292, 204))
        self.assertIsNone(after.quiet)


class StaysRegularTest(unittest.TestCase):
    """The lines that keep their regular price in both halves."""

    def test_a_system_pool_a_line_on_spot_and_an_unpriced_line_stay_as_they_are(self) -> None:
        """An AKS system pool stays regular, a line on spot today keeps its spot price, and an unpriced line stays."""
        system = pool("system", system=True)
        on_spot = Pool("node pool spot", "spot", "node pool", False, (Line(TYPE, Capacity.SPOT, 0.04, ""),))
        unpriced = pool("win", None, note="only Linux machines are priced")
        before = state([system, on_spot, unpriced], [], [])
        self.assertEqual(Spot({TYPE: 0.08}).apply(before), before)

    def test_a_type_with_no_spot_price_stays_regular_with_the_reason(self) -> None:
        """The line keeps its regular price at quiet times too, and says why it has no spot price."""
        today = app(4)
        after = Spot({TYPE: NO_SPOT_PRICE}).apply(today)
        self.assertEqual(after.pools[0].lines[0], replace(today.pools[0].lines[0], spot_note=NO_SPOT_PRICE))
        self.assertEqual((cost(today), cost(after)), (292, 292))
        line = line_results(today, after)[0]
        self.assertEqual((line.spot, line.spot_note), (None, NO_SPOT_PRICE))


class AllStepsTest(unittest.TestCase):
    """The six steps in order, each on what the one before left."""

    def test_all_six_steps_go_from_1168_to_153(self) -> None:
        """Steps 1 to 4 leave two D machines, $263; 8 copies drop to 2 on one at quiet times, $197; it runs spot, $153.

        At quiet times the one machine costs $0.06 an hour on spot. The steps add up to $1,015, the cost today less the
        cost after.
        """
        today = groups_on_a()
        today = replace(today, pods=tuple(replace(pod, workload=WEB) for pod in today.pods))
        offers = [offer(TYPE_A, 0.2, 4, 16), offer(TYPE_D, 0.18, 4, 16), offer(TYPE_B, 0.38, 8, 32)]
        steps: List[Step] = [
            SmallerPods({}, ()),
            SpareMachines(),
            CheaperTypes(search_of(today, offers)),
            PackAcrossPools(),
            FewerCopies(NOTHING),
            Spot({TYPE_A: 0.07, TYPE_D: 0.06, TYPE_B: 0.13}),
        ]
        after, results = Pipeline(steps).run(today)
        figures = [(each.name, each.before, each.after, each.saving) for each in results]
        self.assertEqual(
            figures,
            [
                ("Smaller pods", 1168, 1168, 0),
                ("Spare machines", 1168, 438, 730),
                ("Cheaper machine types", 438, 394, 44),
                ("Packing across pools", 394, 263, 131),
                ("Fewer copies at quiet times", 263, 197, 66),
                ("Spot", 197, 153, 44),
            ],
        )
        self.assertEqual(sum(each.saving for each in results), cost(today) - cost(after))
        self.assertEqual(sum(each.cost_after for each in line_results(today, after)), cost(after))

    def test_every_machine_of_every_step_and_of_the_quiet_hours_holds_its_pods(self) -> None:
        """Six copies of 1000m peak at 1500m, so would ask 1950m: three on a machine is 5850m of 4000m, so web is held.

        At today's requests the two machines stay; at quiet times 2 copies need one, $219, on spot $175. Each state,
        busy and quiet, fits its machines.
        """
        today = app(6, cpu=1000, per_machine=3)
        steps: List[Step] = [
            SmallerPods(peaks(today.pods, 1500, GIB // 2), ()),
            SpareMachines(),
            CheaperTypes(search_of(today, [offer(TYPE, 0.2, 4, 16)])),
            PackAcrossPools(),
            FewerCopies(NOTHING),
            Spot({TYPE: 0.08}),
        ]
        states = [today]
        for step in steps:
            after, _ = Pipeline([step]).run(states[-1])
            states.append(after)
        self.assertEqual([over_full(each) for each in states], [[]] * 7)
        self.assertTrue(all(pod.held for pod in states[-1].pods))
        self.assertEqual([cost(each) for each in states], [292, 292, 292, 292, 292, 219, 175])
