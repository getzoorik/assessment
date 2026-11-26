"""The placement checker: one test for each rule of where Kubernetes would let a pod run."""

import unittest
from dataclasses import replace
from typing import Any, List, Optional, Sequence

from tests.unit.fleet import GIB, api_pod, machine, placed, taint
from zoorik.kubectl.objects import (
    AffinityTerm,
    Container,
    HostPort,
    InitContainer,
    LabelSelector,
    NodeSelectorTerm,
    PersistentVolume,
    Requirement,
    SpreadConstraint,
    Toleration,
)
from zoorik.kubectl.usage import MEBIBYTE
from zoorik.kubernetes.model import Machine, Pod
from zoorik.kubernetes.placement import (
    HOSTNAME,
    OS_LABEL,
    ZONE_LABEL,
    Board,
    Overhead,
    Room,
    bound_to_one_node,
    empty_room,
    fits_machine,
    machine_labels,
    pod_request,
    pool_overhead,
    requests_of,
    requirement_holds,
    selects_namespaces_by_label,
    term_of,
    type_labels,
)

NONE = Overhead({}, frozenset())
WEB = LabelSelector({"app": "web"}, ())


def fits(pod: Pod, target: Machine) -> bool:
    """Tell whether pod fits an empty target by the rules that need no other pod."""
    return fits_machine(pod, target, empty_room(target, NONE))


def board(machines: Sequence[Machine], placed_pods: Sequence[Pod] = ()) -> Board:
    """Make a board over machines, all open, with pods placed on their nodes."""
    made = Board(machines, {each.pool: NONE for each in machines})
    by_name = {each.name: each for each in machines}
    for each in machines:
        made.open(each)
    for pod in placed_pods:
        made.place(pod, by_name[pod.node])
    return made


def allowed(made: Board, pod: Pod, machines: Sequence[Machine]) -> List[str]:
    """Name the machines the board's counted rules allow for pod."""
    rules = made.rules(pod)
    return [each.name for each in machines if made.fits(pod, each, rules)]


def spread(key: str, min_domains: Optional[int] = None, **changes: Any) -> SpreadConstraint:
    """Make a DoNotSchedule constraint of maxSkew 1 over key, for pods labelled app=web."""
    return replace(SpreadConstraint(1, key, "DoNotSchedule", WEB, min_domains, None, None, ()), **changes)


def web(name: str, node: str, constraint: SpreadConstraint, **changes: Any) -> Pod:
    """Place a web replica, labelled app=web, with one spread constraint."""
    return placed(api_pod(name, labels={"app": "web"}, spread=(constraint,), **changes), node)


def term(selector: Optional[LabelSelector], key: str = HOSTNAME, **changes: Any) -> AffinityTerm:
    """Make a required pod affinity or anti-affinity term over key."""
    return replace(AffinityTerm(selector, (), None, key, (), ()), **changes)


class RequestTest(unittest.TestCase):
    """A pod's request as the scheduler counts it."""

    def test_a_pods_request_holds_its_init_phase_sidecars_pod_level_resources_overhead_and_a_slot(self) -> None:
        """Containers and sidecars add up; an init container counts beside the sidecars started before it."""
        source = replace(
            api_pod("job-1", 500, 256 * MEBIBYTE),
            init_containers=(
                InitContainer("shipper", {"cpu": 100}, {}, True),
                InitContainer("setup", {"cpu": 900, "memory": 64 * MEBIBYTE}, {}, False),
            ),
            overhead={"cpu": 250},
        )
        self.assertEqual(
            pod_request(source, requests_of(source), source.requests),
            {"cpu": 1250, "memory": 256 * MEBIBYTE, "pods": 1},
        )
        sized = replace(source, requests={"cpu": 2000})
        self.assertEqual(pod_request(sized, requests_of(sized), sized.requests)["cpu"], 2250)


class FitTest(unittest.TestCase):
    """The rules that need no other pod."""

    def test_every_requested_resource_must_fit(self) -> None:
        """Memory, pod slots and an extended resource count like CPU does."""
        target = machine("m1", allocatable={"cpu": 4000, "memory": 2 * GIB, "pods": 2, "nvidia.com/gpu": 1})
        self.assertTrue(fits(placed(api_pod("a", 100, GIB), "m1"), target))
        self.assertFalse(fits(placed(api_pod("b", 100, 3 * GIB), "m1"), target))
        gpu = replace(api_pod("c"), containers=(Container("app", {"cpu": 100, "nvidia.com/gpu": 2}, {}, ()),))
        self.assertFalse(fits(placed(gpu, "m1"), target))
        room = Room({"cpu": 4000, "memory": 2 * GIB, "pods": 0}, set())
        self.assertFalse(fits_machine(placed(api_pod("d"), "m1"), target, room))

    def test_attach_limits_count_each_volume_once(self) -> None:
        """A driver that attaches one volume takes a pod whose volume is already attached, and no other."""
        target = machine("m1", attach={"disk.csi.azure.com": 1})
        shared = PersistentVolume("pv-one", "disk.csi.azure.com", None, None)
        room = Room(dict(target.allocatable), set(), {"disk.csi.azure.com": {"pv-one"}})
        self.assertTrue(fits_machine(placed(api_pod("a"), "m1", volumes=(shared,)), target, room))
        other = PersistentVolume("pv-two", "disk.csi.azure.com", None, None)
        self.assertFalse(fits_machine(placed(api_pod("b"), "m1", volumes=(other,)), target, room))

    def test_the_node_selector_must_match(self) -> None:
        """A pod that selects a label goes only where the label has that value."""
        pod = placed(api_pod("a", node_selector={"tier": "db"}), "m1")
        self.assertTrue(fits(pod, machine("m1", labels={"tier": "db"})))
        self.assertFalse(fits(pod, machine("m2", labels={"tier": "web"})))

    def test_one_term_of_the_required_node_affinity_must_match(self) -> None:
        """Terms are alternatives, a term's requirements all hold, matchFields names the node; an empty term fails."""
        terms = (
            NodeSelectorTerm((Requirement("tier", "In", ("db",)), Requirement("disk", "Exists", ())), ()),
            NodeSelectorTerm((), (Requirement("metadata.name", "In", ("m3",)),)),
        )
        pod = placed(api_pod("a", node_affinity=terms), "m1")
        self.assertTrue(fits(pod, machine("m1", labels={"tier": "db", "disk": "ssd"})))
        self.assertFalse(fits(pod, machine("m2", labels={"tier": "db"})))
        self.assertTrue(fits(pod, machine("m3")))
        self.assertFalse(fits(placed(api_pod("b", node_affinity=(NodeSelectorTerm((), ()),)), "m1"), machine("m1")))

    def test_each_operator_reads_as_the_api_server_reads_it(self) -> None:
        """NotIn and DoesNotExist hold for a missing label; Gt and Lt compare whole numbers only."""
        self.assertTrue(requirement_holds(Requirement("k", "NotIn", ("a",)), None))
        self.assertTrue(requirement_holds(Requirement("k", "DoesNotExist", ()), None))
        self.assertFalse(requirement_holds(Requirement("k", "In", ("a",)), None))
        self.assertTrue(requirement_holds(Requirement("k", "Gt", ("3",)), "4"))
        self.assertFalse(requirement_holds(Requirement("k", "Lt", ("3",)), "x"))

    def test_no_schedule_and_no_execute_taints_need_a_toleration(self) -> None:
        """PreferNoSchedule is a preference; Exists tolerates any value; Equal needs the value."""
        pod = placed(api_pod("a"), "m1")
        self.assertTrue(fits(pod, machine("m1", taints=(taint("soft", "PreferNoSchedule"),))))
        self.assertFalse(fits(pod, machine("m2", taints=(taint("gpu", "NoExecute"),))))
        tolerant = placed(api_pod("b", tolerations=(Toleration("gpu", "Exists", None, None),)), "m1")
        self.assertTrue(fits(tolerant, machine("m2", taints=(taint("gpu", "NoExecute", "a100"),))))
        equal = placed(api_pod("c", tolerations=(Toleration("gpu", "Equal", "t4", "NoSchedule"),)), "m1")
        self.assertFalse(fits(equal, machine("m3", taints=(taint("gpu", "NoSchedule", "a100"),))))

    def test_every_volume_must_reach_the_machine(self) -> None:
        """A volume bound to a zone keeps its pod in that zone."""
        zone = NodeSelectorTerm((Requirement(ZONE_LABEL, "In", ("zone-1",)),), ())
        pod = placed(api_pod("a"), "m1", volumes=(PersistentVolume("pv", "disk.csi.azure.com", None, (zone,)),))
        self.assertTrue(fits(pod, machine("m1", "zone-1")))
        self.assertFalse(fits(pod, machine("m2", "zone-2")))

    def test_host_ports_must_be_free(self) -> None:
        """A port another pod took on the machine keeps a pod that takes it too off the machine."""
        ported = replace(api_pod("a"), containers=(Container("app", {"cpu": 100}, {}, (HostPort(9100, "TCP"),)),))
        target = machine("m1")
        self.assertTrue(fits(placed(ported, "m1"), target))
        taken = Room(dict(target.allocatable), {HostPort(9100, "TCP")})
        self.assertFalse(fits_machine(placed(ported, "m1"), target, taken))

    def test_the_machine_runs_the_os_and_architecture_of_the_pods_machine_today(self) -> None:
        """A Linux pod on amd64 doesn't go on a Windows machine or an arm64 one."""
        pod = placed(api_pod("a"), "m1")
        self.assertFalse(fits(pod, machine("m2", labels={OS_LABEL: "windows"})))
        self.assertFalse(fits(pod, machine("m3", labels={"kubernetes.io/arch": "arm64"})))


class SpreadTest(unittest.TestCase):
    """DoNotSchedule topology spread, counted over every machine, empty ones included."""

    def test_spread_over_machines_holds_the_skew(self) -> None:
        """With one replica on m1 and none on m2, a second goes to m2 only."""
        machines = [machine("m1"), machine("m2")]
        made = board(machines, [web("web-1", "m1", spread(HOSTNAME))])
        self.assertEqual(allowed(made, web("web-2", "m1", spread(HOSTNAME)), machines), ["m2"])

    def test_an_empty_domain_counts(self) -> None:
        """Zones 1 and 2 hold a replica each and zone 3 none, so the third goes to zone 3."""
        machines = [machine("m1", "zone-1"), machine("m2", "zone-2"), machine("m3", "zone-3")]
        zone = spread(ZONE_LABEL)
        made = board(machines, [web("web-1", "m1", zone), web("web-2", "m2", zone)])
        self.assertEqual(allowed(made, web("web-3", "m1", zone), machines), ["m3"])

    def test_fewer_domains_than_min_domains_make_the_minimum_0(self) -> None:
        """Two zones hold a replica each; with minDomains 3 the global minimum is 0, so no zone takes a second."""
        machines = [machine("m1", "zone-1"), machine("m2", "zone-2")]
        zone = spread(ZONE_LABEL, 3)
        made = board(machines, [web("web-1", "m1", zone), web("web-2", "m2", zone)])
        self.assertEqual(allowed(made, web("web-3", "m1", zone), machines), [])
        self.assertEqual(allowed(made, web("web-3", "m1", spread(ZONE_LABEL)), machines), ["m1", "m2"])

    def test_a_tainted_machine_counts_unless_node_taints_policy_is_honor(self) -> None:
        """Replicas on m1 and m3: the tainted, empty m2 keeps the minimum at 0 unless its taint rules it out."""
        machines = [machine("m1"), machine("m2", taints=(taint("db"),)), machine("m3")]
        for constraint, expected in (
            (spread(HOSTNAME), []),
            (spread(HOSTNAME, node_taints_policy="Honor"), ["m1", "m3"]),
        ):
            with self.subTest(policy=constraint.node_taints_policy):
                made = board(machines, [web("web-1", "m1", constraint), web("web-2", "m3", constraint)])
                self.assertEqual(allowed(made, web("web-3", "m1", constraint), machines), expected)

    def test_a_machine_the_pod_cant_select_counts_only_when_node_affinity_policy_is_ignore(self) -> None:
        """The pod selects tier=db, which only m3 has: m1 and m2 count, at 0, only when the policy is Ignore."""
        machines = [machine("m1"), machine("m2"), machine("m3", labels={"tier": "db"})]
        select = {"tier": "db"}
        for constraint, expected in ((spread(HOSTNAME), ["m3"]), (spread(HOSTNAME, node_affinity_policy="Ignore"), [])):
            with self.subTest(policy=constraint.node_affinity_policy):
                made = board(machines, [web("web-1", "m3", constraint, node_selector=select)])
                self.assertEqual(
                    allowed(made, web("web-2", "m3", constraint, node_selector=select), machines), expected
                )

    def test_match_label_keys_count_only_pods_of_the_same_value(self) -> None:
        """A replica of another revision doesn't count against this revision's spread."""
        keyed = spread(HOSTNAME, match_label_keys=("pod-template-hash",))
        machines = [machine("m1"), machine("m2")]
        old = placed(api_pod("web-1", labels={"app": "web", "pod-template-hash": "old"}, spread=(keyed,)), "m1")
        new = placed(api_pod("web-2", labels={"app": "web", "pod-template-hash": "new"}, spread=(keyed,)), "m1")
        self.assertEqual(allowed(board(machines, [old]), new, machines), ["m1", "m2"])

    def test_a_machine_without_the_topology_key_takes_no_spread_pod(self) -> None:
        """A machine with no zone label is no domain, so a pod spread over zones can't go there."""
        bare = replace(machine("m2"), labels={HOSTNAME: "m2", OS_LABEL: "linux", "kubernetes.io/arch": "amd64"})
        machines = [machine("m1"), bare]
        self.assertEqual(allowed(board(machines), web("web-1", "m1", spread(ZONE_LABEL)), machines), ["m1"])


class PodAffinityTest(unittest.TestCase):
    """Required pod affinity and anti-affinity, both ways."""

    def test_required_affinity_goes_where_a_target_runs(self) -> None:
        """A pod that needs the cache beside it goes to the cache's machine only."""
        machines = [machine("m1"), machine("m2")]
        made = board(machines, [placed(api_pod("cache-1", labels={"app": "cache"}), "m2")])
        follower = placed(api_pod("app-1", affinity=(term(LabelSelector({"app": "cache"}, ())),)), "m1")
        self.assertEqual(allowed(made, follower, machines), ["m2"])

    def test_the_first_pod_of_a_group_that_matches_its_own_term_goes_anywhere(self) -> None:
        """No pod matches yet, and the pod matches its own term, so it may go on any machine."""
        machines = [machine("m1"), machine("m2")]
        first = placed(api_pod("web-1", labels={"app": "web"}, affinity=(term(WEB),)), "m1")
        self.assertEqual(allowed(board(machines), first, machines), ["m1", "m2"])
        stranger = placed(api_pod("app-1", affinity=(term(WEB),)), "m1")
        self.assertEqual(allowed(board(machines), stranger, machines), [])

    def test_a_pods_anti_affinity_keeps_it_from_the_pods_it_selects(self) -> None:
        """A pod that must not share a machine with web goes where web is not."""
        machines = [machine("m1"), machine("m2")]
        made = board(machines, [placed(api_pod("web-1", labels={"app": "web"}), "m1")])
        apart = placed(api_pod("guard", anti_affinity=(term(WEB),)), "m1")
        self.assertEqual(allowed(made, apart, machines), ["m2"])

    def test_a_placed_pods_anti_affinity_keeps_others_from_its_machine(self) -> None:
        """The guard on m1 must not share its machine with web, so web goes to m2."""
        machines = [machine("m1"), machine("m2")]
        made = board(machines, [placed(api_pod("guard", anti_affinity=(term(WEB),)), "m1")])
        self.assertEqual(allowed(made, placed(api_pod("web-1", labels={"app": "web"}), "m2"), machines), ["m2"])

    def test_a_term_with_a_namespace_selector_covers_every_namespace(self) -> None:
        """The run reads no namespace labels, so a selector-based term counts pods of any namespace."""
        selector = LabelSelector({"team": "one"}, ())
        owner = api_pod("app-1", affinity=(term(WEB, namespace_selector=selector),))
        self.assertTrue(
            term_of(owner.affinity[0], owner).covers(
                replace(api_pod("web-1"), namespace="other", labels={"app": "web"})
            )
        )
        self.assertTrue(selects_namespaces_by_label(owner))
        empty = api_pod("app-2", affinity=(term(WEB, namespace_selector=LabelSelector({}, ())),))
        self.assertFalse(selects_namespaces_by_label(empty))


class MachineTest(unittest.TestCase):
    """Machine labels, overhead, and what binds a volume to one node."""

    def test_a_machine_shows_its_types_labels_as_its_lines_nodes_show_them(self) -> None:
        """A type label every node of the line shows stays; one they show apart names no one value."""
        nodes = [
            {"node.kubernetes.io/instance-type": "Standard_D4s_v5", "feature.node.kubernetes.io/cpu-avx": "true"},
            {"node.kubernetes.io/instance-type": "Standard_D4s_v5", "feature.node.kubernetes.io/cpu-avx": "false"},
        ]
        shared = type_labels(nodes)
        self.assertEqual(shared, {"node.kubernetes.io/instance-type": "Standard_D4s_v5"})
        own = {HOSTNAME: "m1", "feature.node.kubernetes.io/cpu-avx": "true", "tier": "db"}
        self.assertEqual(machine_labels(own, shared), {HOSTNAME: "m1", "tier": "db", **shared})

    def test_a_pools_overhead_is_each_daemon_set_once_and_its_largest_static_pods(self) -> None:
        """Two pods of one DaemonSet count once, at the larger; the larger static total counts; each holds a slot."""
        daemons = [
            placed(api_pod("agent-1", 100, 64 * MEBIBYTE, ("DaemonSet", "agent")), "m1"),
            placed(api_pod("agent-2", 150, 32 * MEBIBYTE, ("DaemonSet", "agent")), "m2"),
        ]
        statics = [
            placed(api_pod("proxy-m1", 200, controller=("Node", "m1")), "m1"),
            placed(api_pod("probe-m1", 50, controller=("Node", "m1")), "m1"),
            placed(api_pod("proxy-m2", 100, controller=("Node", "m2")), "m2"),
        ]
        overhead = pool_overhead([*daemons, *statics])
        self.assertEqual(dict(overhead.requests), {"cpu": 400, "memory": 320 * MEBIBYTE, "pods": 3})
        self.assertEqual(empty_room(machine("m1"), overhead).free["cpu"], 3600)

    def test_a_volume_bound_to_a_node_names_its_hostname(self) -> None:
        """A local volume's required node affinity names the node's hostname; a zone doesn't bind it to one node."""
        local = NodeSelectorTerm((Requirement(HOSTNAME, "In", ("m1",)),), ())
        zone = NodeSelectorTerm((Requirement(ZONE_LABEL, "In", ("zone-1",)),), ())
        self.assertTrue(bound_to_one_node(PersistentVolume("pv-a", None, None, (local,))))
        self.assertFalse(bound_to_one_node(PersistentVolume("pv-b", "disk.csi.azure.com", None, (zone,))))
        self.assertFalse(bound_to_one_node(PersistentVolume("pv-c", None, None, None)))
