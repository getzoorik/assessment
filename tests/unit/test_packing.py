"""Packing: the order pods go in, the zones machines open in, and the cap."""

import unittest
from fractions import Fraction

from tests.unit.fleet import api_pod, machine, placed
from zoorik.kubectl.objects import AffinityTerm, LabelSelector
from zoorik.kubernetes.packing import Job, Opener, largest_share, order, pack
from zoorik.kubernetes.placement import HOSTNAME, Overhead

NONE = Overhead({}, frozenset())


class OrderTest(unittest.TestCase):
    """Largest first, a pod with required affinity after the pods it selects."""

    def test_the_largest_share_of_a_machine_goes_first(self) -> None:
        """A pod's size is its largest share of the room, over every resource the room has."""
        self.assertEqual(largest_share({"cpu": 1000, "memory": 1024}, {"cpu": 4000, "memory": 2048}), Fraction(1, 2))
        small, large = placed(api_pod("a-small", 100), "m1"), placed(api_pod("b-large", 2000), "m1")
        room = {"cpu": 4000, "memory": 16 * 2**30, "pods": 110}
        self.assertEqual([pod.source.name for pod in order([small, large], room)], ["b-large", "a-small"])

    def test_a_pod_with_required_affinity_goes_after_its_targets(self) -> None:
        """The follower is larger, but needs the cache placed first."""
        near = AffinityTerm(LabelSelector({"app": "cache"}, ()), (), None, HOSTNAME, (), ())
        follower = placed(api_pod("follower", 2000, affinity=(near,)), "m1")
        cache = placed(api_pod("cache", 100, labels={"app": "cache"}), "m1")
        room = {"cpu": 4000, "memory": 16 * 2**30, "pods": 110}
        self.assertEqual([pod.source.name for pod in order([follower, cache], room)], ["cache", "follower"])


class OpenerTest(unittest.TestCase):
    """Machines open zone by zone."""

    def test_each_opening_starts_at_the_zone_after_the_last(self) -> None:
        """Two machines in each of two zones open a, c, b, d: the zones in turn."""
        machines = [machine("a", "zone-1"), machine("b", "zone-1"), machine("c", "zone-2"), machine("d", "zone-2")]
        opener = Opener(machines)
        opened = [opener.open(lambda _: True) for _ in range(4)]
        self.assertEqual([each.name for each in opened if each is not None], ["a", "c", "b", "d"])
        self.assertIsNone(opener.open(lambda _: True))


class PackTest(unittest.TestCase):
    """A packing holds every pod, within the cap."""

    def test_a_packing_that_needs_more_machines_than_the_cap_gives_none(self) -> None:
        """Two pods of 3000m need two machines, and the cap is one."""
        machines = (machine("m1"), machine("m2"))
        pods = (placed(api_pod("a", 3000), "m1"), placed(api_pod("b", 3000), "m2"))
        job = Job(pods, (), (), machines, (), {"node pool apps": NONE}, 1)
        self.assertIsNone(pack(job))
        packing = pack(Job(pods, (), (), machines, (), {"node pool apps": NONE}, 2))
        self.assertIsNotNone(packing)
