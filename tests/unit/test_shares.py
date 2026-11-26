"""The workload table: a row's saving split by the CPU and memory each workload frees."""

import unittest
from dataclasses import replace

from tests.unit.fleet import api_pod, machine, placed, pool, state
from zoorik.kubectl.usage import MEBIBYTE
from zoorik.kubernetes.model import Workload
from zoorik.kubernetes.shares import Freed, split, workload_results

A = Workload("Deployment", "shop", "a")
B = Workload("Deployment", "shop", "b")
C = Workload("Deployment", "shop", "c")


class SplitTest(unittest.TestCase):
    """Shares rounded down, the dollars left to the largest remainders."""

    def test_the_shares_add_up_to_the_saving(self) -> None:
        """Half of each one's CPU share plus half of its memory share: 133.83 and 158.17 of 292 give 134 and 158."""
        freed = {A: Freed(2000, 4096 * MEBIBYTE), B: Freed(1000, 12288 * MEBIBYTE)}
        self.assertEqual(split(292, freed), {A: 134, B: 158})

    def test_one_resource_freed_alone_weighs_alone(self) -> None:
        """No memory is freed, so the CPU shares weigh alone: 3/4 and 1/4 of 100."""
        self.assertEqual(split(100, {A: Freed(300, 0), B: Freed(100, 0)}), {A: 75, B: 25})

    def test_equal_remainders_go_by_namespace_and_name(self) -> None:
        """Three equal thirds of 100 leave one dollar, which goes to the first by name."""
        freed = {C: Freed(100, 0), B: Freed(100, 0), A: Freed(100, 0)}
        self.assertEqual(split(100, freed), {A: 34, B: 33, C: 33})

    def test_nothing_freed_or_nothing_saved_gives_no_share(self) -> None:
        """A saving of $0 gives every workload $0, and so does a saving where nothing is freed."""
        self.assertEqual(split(0, {A: Freed(100, 0)}), {A: 0})
        self.assertEqual(split(50, {A: Freed(0, 0)}), {A: 0})


class WorkloadResultsTest(unittest.TestCase):
    """What each workload frees, from today's requests to after's."""

    def test_a_raised_workload_frees_nothing_and_takes_no_share(self) -> None:
        """A frees 200m; B was raised, so it counts as freeing nothing and takes no share of the $100."""
        today = state(
            [pool()],
            [machine("m1")],
            [placed(api_pod("a-1", 500), "m1", workload=A), placed(api_pod("b-1", 500), "m1", workload=B)],
        )
        after_pods = (
            replace(today.pods[0], request={**today.pods[0].request, "cpu": 300}),
            replace(today.pods[1], request={**today.pods[1].request, "cpu": 900}, raised=True),
        )
        results = workload_results(today, replace(today, pods=after_pods), 100)
        self.assertEqual(
            [(each.name, each.cpu, each.memory, each.raised, each.share) for each in results],
            [("a", 200, 0, False, 100), ("b", 0, 0, True, 0)],
        )
