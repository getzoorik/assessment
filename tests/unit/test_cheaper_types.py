"""Step 3, cheaper machine types: the figure worked out by hand, the filters, the search and its speed.

The candidate filters, the pools that keep their type, the mixes of two types, spot pools, and the search on a large
pool.
"""

import time
import unittest
from dataclasses import replace
from typing import Any, Dict, List, Sequence, Tuple
from unittest import mock

from tests.unit.fleet import APPS, GIB, TYPE, api_pod, machine, placed, pool, state, taint
from zoorik.cloud.azure import AgentPool, ComputeSku
from zoorik.cloud.keys import AZURE
from zoorik.kubectl.objects import Container, Node, NodeSelectorTerm, PersistentVolume, Requirement, Toleration
from zoorik.kubectl.usage import MEBIBYTE
from zoorik.kubernetes.allocatable import ClusterNodes, TypeRoom
from zoorik.kubernetes.assessment import candidate_keys, searches
from zoorik.kubernetes.catalogue import AZURE_DISK, MachineType, azure_type
from zoorik.kubernetes.collect import AGENT_POOL_LABEL, TYPE_LABEL
from zoorik.kubernetes.model import ClusterId, ClusterState, Line, Machine, Pod, Pool, Workload
from zoorik.kubernetes.packing import Job, pack
from zoorik.kubernetes.steps import Pipeline, cost
from zoorik.kubernetes.steps import cheaper_types as step3
from zoorik.kubernetes.steps.cheaper_types import (
    MODE_LABEL,
    ONE_TYPE_KINDS,
    Candidate,
    CheaperTypes,
    Offer,
    Plan,
    Search,
    plans,
    searchable,
    zones_of,
)
from zoorik.pricing.machines import Capacity

REGION = "westeurope"
ZONE = "westeurope-1"
D8S = "Standard_D8s_v5"
NODE_POOL = "NodePool burst"
# A D4s v5 node as AKS 1.30 reports it with 110 max pods: its capacity, and what the reservation leaves allocatable.
D4S_CAPACITY = {"cpu": 4000, "memory": 16393364 * 1024, "pods": 110}
D4S_ALLOCATABLE = {"cpu": 3860, "memory": 13986964 * 1024, "pods": 110}


def offer(name: str, hourly: float, cores: int, memory_gib: int) -> Offer:
    """Make an amd64 offer at its hourly price, giving pods its cores, its GiB and 110 pod slots."""
    return Offer(name, "amd64", hourly, {"cpu": cores * 1000, "memory": memory_gib * GIB, "pods": 110}, {})


def search_of(
    before: ClusterState, offers: Sequence[Offer], capacity: Capacity = Capacity.REGULAR
) -> Dict[str, Search]:
    """Give each pool a search among offers, in the zones its machines use, by pool id."""
    return {
        each.id: Search(tuple(offers), zones_of(before, each), capacity, each.kind in ONE_TYPE_KINDS)
        for each in before.pools
    }


def kind(name: str, vcpus: int = 4, memory_gib: int = 16, **changes: Any) -> MachineType:
    """Make an Azure VM size of vcpus and memory_gib: amd64, premium disks, offered in zone 1 alone, with changes."""
    made = MachineType(
        name=name,
        vcpus=vcpus,
        memory_mib=memory_gib * 1024,
        architecture="amd64",
        burstable=False,
        gpus=0,
        zones=frozenset({ZONE}),
        regional=False,
        premium_io=True,
        ultra_zones=frozenset(),
        ultra_regional=False,
        attach={},
        pods=None,
    )
    return replace(made, **changes)


def aks_machine(name: str, pool_name: str = "apps", **changes: Any) -> Machine:
    """Make a D4s v5 machine of an AKS User pool in zone 1, with the labels that name its pool."""
    labels = {AGENT_POOL_LABEL: pool_name, "agentpool": pool_name, MODE_LABEL: "user"}
    made = machine(name, ZONE, f"node pool {pool_name}", labels, allocatable=D4S_ALLOCATABLE)
    return replace(made, **changes)


def aks_cluster(before: ClusterState) -> ClusterNodes:
    """Give an AKS cluster of D4s v5 nodes at 1.30 whose allocatable follows the reservation, each maxPods 110."""
    nodes = {
        each.name: Node(each.name, each.labels, each.taints, None, D4S_CAPACITY, each.allocatable, "v1.30.4")
        for each in before.machines
    }
    agent_pools = {each.name: AgentPool(each.name, "User", None, 110) for each in before.pools}
    return ClusterNodes(AZURE, before, nodes, agent_pools)


def apps_cluster(volumes: Sequence[PersistentVolume] = ()) -> ClusterState:
    """Make the pool apps of two D4s v5 machines in zone 1, one pod of 500m and 1 GiB on the first with volumes."""
    pods = [placed(api_pod("web-1", 500, GIB), "m1", volumes=tuple(volumes))]
    return state([pool()], [aks_machine("m1"), aks_machine("m2")], pods)


def candidates(before: ClusterState, types: Sequence[MachineType]) -> List[str]:
    """Name the candidates plans gives the pool apps among types and its own D4s v5, which has no Ultra Disk."""
    listed = {each.name.lower(): each for each in (kind(TYPE), *types)}
    planned = plans(aks_cluster(before), searchable(before), listed)
    assert not isinstance(planned, str), planned
    return [each.kind.name for each in planned[APPS].candidates]


class WorkedFigureTest(unittest.TestCase):
    """The figure the plan works out by hand."""

    def test_two_machines_at_020_become_one_at_030(self) -> None:
        """Two D4s v5 at $0.20 hold four pods of 1500m and 4 GiB, and so does one D8s v5 at $0.30: $292 to $219."""
        pods = [placed(api_pod(f"p-{index}", 1500, 4 * GIB), f"m{1 + index // 2}") for index in range(4)]
        before = state([pool()], [machine("m1"), machine("m2")], pods)
        step = CheaperTypes(search_of(before, [offer(TYPE, 0.2, 4, 16), offer(D8S, 0.3, 8, 32)]))
        after, results = Pipeline([step]).run(before)
        figures = [(each.name, each.row, each.before, each.after, each.saving) for each in results]
        self.assertEqual(figures, [("Cheaper machine types", "cheaper machine types", 292, 219, 73)])
        self.assertEqual([(each.pool, each.machine_type) for each in after.machines], [(APPS, D8S)])
        self.assertEqual({pod.node for pod in after.pods}, {after.machines[0].name})


class CandidateFilterTest(unittest.TestCase):
    """A type is a candidate for a pool only when it passes every filter."""

    def test_each_filter_leaves_out_its_type(self) -> None:
        """Arm64, a burstable size, a GPU, zones the pool doesn't use, a restricted size and one below AKS's minimum."""
        restricted = azure_type(
            REGION,
            ComputeSku(
                resource_type="virtualMachines",
                name="Standard_D4as_v5",
                family="standardDASv5Family",
                capabilities={"vCPUs": "4", "MemoryGB": "16", "CpuArchitectureType": "x64", "PremiumIO": "True"},
                zones=frozenset(),
                offered=False,
                ultra_ssd_zones=frozenset(),
            ),
        )
        assert restricted is not None
        types = [
            kind("Standard_F4s_v2", memory_gib=8),
            kind("Standard_D4ps_v5", architecture="arm64"),
            kind("Standard_B4ms", burstable=True),
            kind("Standard_NC4as_T4_v3", memory_gib=28, gpus=1),
            kind("Standard_E4s_v5", memory_gib=32, zones=frozenset({"westeurope-2", "westeurope-3"})),
            restricted,
            kind("Standard_A1_v2", vcpus=1, memory_gib=2),
            kind("Standard_A2_v2", vcpus=2, memory_gib=4),
        ]
        self.assertEqual(candidates(apps_cluster(), types), ["Standard_A2_v2", TYPE, "Standard_F4s_v2"])

    def test_a_pool_in_no_zone_needs_a_type_offered_in_the_region(self) -> None:
        """A machine in no zone takes a type the subscription may use in the region; a restricted one is left out."""
        machines = [aks_machine("m1", zone="0"), aks_machine("m2", zone="0")]
        before = state([pool()], machines, [placed(api_pod("web-1", 500, GIB), "m1")])
        types = [kind("Standard_F4s_v2", memory_gib=8, regional=True), kind("Standard_D4as_v5", regional=False)]
        self.assertEqual(candidates(before, types), ["Standard_F4s_v2"])

    def test_premium_disks_need_a_type_that_takes_them(self) -> None:
        """A Premium SSD and an in-tree azureDisk need PremiumIO; a Standard SSD and a CSI disk with no tier don't."""
        without = kind("Standard_D4_v5", premium_io=False)
        cases = {
            "Premium SSD": (PersistentVolume("pv", AZURE_DISK, "Premium_LRS", None, False), [TYPE]),
            "in-tree azureDisk": (PersistentVolume("pv", AZURE_DISK, None, None, True), [TYPE]),
            "Standard SSD": (PersistentVolume("pv", AZURE_DISK, "StandardSSD_LRS", None, False), [without.name, TYPE]),
            "CSI disk with no tier": (PersistentVolume("pv", AZURE_DISK, None, None, False), [without.name, TYPE]),
        }
        for name, (volume, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(candidates(apps_cluster([volume]), [without]), expected)

    def test_ultra_disk_needs_a_type_that_offers_it_in_the_pools_zones(self) -> None:
        """Only a type with Ultra Disk in zone 1 is a candidate for a pool in zone 1 whose pod uses one."""
        ultra = PersistentVolume("pv", AZURE_DISK, "UltraSSD_LRS", None, False)
        types = [
            kind("Standard_E4s_v5", memory_gib=32, ultra_zones=frozenset({ZONE})),
            kind("Standard_F4s_v2", memory_gib=8, ultra_zones=frozenset({"westeurope-2"})),
        ]
        self.assertEqual(candidates(apps_cluster([ultra]), types), ["Standard_E4s_v5"])


class KeepTypeTest(unittest.TestCase):
    """The pools that keep their machine type."""

    def test_a_gpu_pool_keeps_its_type(self) -> None:
        """A pool of a GPU size gets no plan; the pool beside it gets its own."""
        gpu = "Standard_NC4as_T4_v3"
        gpu_pool = Pool("node pool gpu", "gpu", "node pool", False, (Line(gpu, Capacity.REGULAR, 0.5, ""),))
        machines = [aks_machine("m1"), aks_machine("m2"), aks_machine("g1", "gpu", machine_type=gpu)]
        pods = [placed(api_pod("web-1", 500, GIB), "m1"), placed(api_pod("train-1", 500, GIB), "g1", "node pool gpu")]
        before = state([pool(), gpu_pool], machines, pods)
        listed = {each.name.lower(): each for each in (kind(TYPE), kind(gpu, memory_gib=28, gpus=1))}
        planned = plans(aks_cluster(before), searchable(before), listed)
        assert not isinstance(planned, str), planned
        self.assertEqual(sorted(planned), [APPS])

    def test_a_pod_that_selects_its_type_or_asks_for_what_comes_with_it_keeps_the_pool(self) -> None:
        """A pod that selects the type by its label or a Karpenter key, or asks for a GPU or hugepages, keeps it.

        So does a pool that has no price; a plain pod leaves the pool free to change.
        """
        family = Requirement("karpenter.k8s.aws/instance-family", "In", ("m6i",))
        cases = {
            TYPE_LABEL: (api_pod("web-1", node_selector={TYPE_LABEL: TYPE}), pool()),
            "karpenter.k8s.aws/instance-family": (
                api_pod("web-1", node_affinity=(NodeSelectorTerm((family,), ()),)),
                pool(),
            ),
            "nvidia.com/gpu": (
                api_pod("web-1", containers=(Container("app", {"cpu": 100, "nvidia.com/gpu": 1}, {}, ()),)),
                pool(),
            ),
            "hugepages-2Mi": (
                api_pod("web-1", containers=(Container("app", {"cpu": 100, "hugepages-2Mi": 512 * MEBIBYTE}, {}, ()),)),
                pool(),
            ),
            "no price": (api_pod("web-1"), pool(hourly=None, note="no list price for Standard_D4s_v5 in westeurope")),
        }
        for name, (source, kept) in cases.items():
            with self.subTest(name):
                self.assertEqual(searchable(state([kept], [machine("m1")], [placed(source, "m1")])), ())
        free = searchable(state([pool()], [machine("m1")], [placed(api_pod("web-1"), "m1")]))
        self.assertEqual([each.pool.id for each in free], [APPS])


def three_pods(sizes: Tuple[int, int, int], pool_id: str = APPS, **changes: Any) -> List[Pod]:
    """Make pods p-1, p-2 and p-3 of 2 GiB and sizes millicores on m1, m2 and m3 of pool_id, each with changes."""
    return [
        placed(api_pod(f"p-{index}", cpu, 2 * GIB, **changes), f"m{index}", pool_id)
        for index, cpu in enumerate(sizes, start=1)
    ]


class MixTest(unittest.TestCase):
    """Mixes of two types: where they beat each single type, and the new pool a one-type pool gets."""

    OFFERS = (offer(TYPE, 0.2, 4, 16), offer(D8S, 0.36, 8, 32))

    def test_a_mix_of_two_types_beats_every_single_type(self) -> None:
        """Three pods of 3500m need three D4s v5 ($438) or two D8s v5 ($526); one of each holds them: $409.

        A Karpenter NodePool holds both types itself.
        """
        today = Pool(NODE_POOL, "burst", "NodePool", False, (Line("Standard_D4as_v5", Capacity.REGULAR, 0.25, ""),))
        machines = [machine(f"m{index}", pool=NODE_POOL, machine_type="Standard_D4as_v5") for index in (1, 2, 3)]
        before = state([today], machines, three_pods((3500, 3500, 3500), NODE_POOL))
        after, [result] = Pipeline([CheaperTypes(search_of(before, self.OFFERS))]).run(before)
        self.assertEqual((result.before, result.after), (548, 409))
        self.assertEqual(
            sorted((each.pool, each.machine_type) for each in after.machines), [(NODE_POOL, TYPE), (NODE_POOL, D8S)]
        )
        self.assertEqual([each.id for each in after.pools], [NODE_POOL])

    def test_on_an_agent_pool_the_second_type_is_a_new_user_pool_without_the_pools_name(self) -> None:
        """The D8s v5 goes to a new pool beside apps: apps' other labels and taints, no name labels, User mode."""
        dedicated = taint("dedicated", value="shop")
        machines = [aks_machine(f"m{index}", taints=(dedicated,)) for index in (1, 2, 3)]
        machines = [replace(each, labels={**each.labels, "team": "shop"}) for each in machines]
        tolerates = (Toleration("dedicated", "Equal", "shop", "NoSchedule"),)
        before = state([pool()], machines, three_pods((3500, 3500, 3500), tolerations=tolerates))
        after, [result] = Pipeline([CheaperTypes(search_of(before, self.OFFERS))]).run(before)
        self.assertEqual((result.before, result.after), (438, 409))
        new = after.pools[1]
        self.assertEqual(
            (new.id, new.name, new.kind, new.system, new.split_from),
            (f"new {APPS}", "apps", "new node pool", False, APPS),
        )
        [added] = [each for each in after.machines if each.pool == new.id]
        self.assertEqual(added.machine_type, D8S)
        self.assertEqual(
            (added.labels.get("team"), added.labels.get(MODE_LABEL), added.taints), ("shop", "user", (dedicated,))
        )
        self.assertNotIn(AGENT_POOL_LABEL, added.labels)
        self.assertNotIn("agentpool", added.labels)
        self.assertEqual([each.machine_type for each in after.machines if each.pool == APPS], [TYPE])

    def test_a_pod_that_selects_the_pools_name_stays_on_the_first_type(self) -> None:
        """p-1 selects the pool by name: it stays on a D4s v5 of apps while the others move to the new pool.

        When p-1 is on a machine the mix would empty, the new pool can't take it, and the pool stays as it is.
        """
        by_name = {AGENT_POOL_LABEL: "apps"}
        fullest = three_pods((3800, 3500, 3500))
        fullest[0] = replace(fullest[0], source=replace(fullest[0].source, node_selector=by_name))
        before = state([pool()], [aks_machine(f"m{index}") for index in (1, 2, 3)], fullest)
        after, [result] = Pipeline([CheaperTypes(search_of(before, self.OFFERS))]).run(before)
        self.assertEqual((result.before, result.after), (438, 409))
        machines = {each.name: each for each in after.machines}
        pools = {pod.source.name: machines[pod.node].pool for pod in after.pods}
        self.assertEqual(pools, {"p-1": APPS, "p-2": f"new {APPS}", "p-3": f"new {APPS}"})
        emptied = three_pods((3500, 3500, 3800))
        emptied[0] = replace(emptied[0], source=replace(emptied[0].source, node_selector=by_name))
        kept = state([pool()], [aks_machine(f"m{index}") for index in (1, 2, 3)], emptied)
        after, [result] = Pipeline([CheaperTypes(search_of(kept, self.OFFERS))]).run(kept)
        self.assertEqual((result.before, result.after, after), (438, 438, kept))


class CoverTest(unittest.TestCase):
    """A type that another tried before it covers, at least as much room for no more, is never packed."""

    def test_types_that_give_no_more_for_no_less_count_once(self) -> None:
        """Three pods of 2500m pack onto three D4as v5 ($438), not the two their lower bound says, then one D8s v5.

        The D4s v5 gives the same room at the same price and the D4s v3 less memory, so neither is packed; the search
        stops at the dearer D4s v4, whose lower bound reaches the D8s v5's $292.
        """
        before = state([pool()], [machine(f"m{index}") for index in (1, 2, 3)], three_pods((2500, 2500, 2500)))
        offers = [
            offer(TYPE, 0.2, 4, 16),
            offer("Standard_D4as_v5", 0.2, 4, 16),
            offer("Standard_D4s_v3", 0.2, 4, 14),
            offer("Standard_D4s_v4", 0.22, 4, 16),
            offer(D8S, 0.4, 8, 32),
        ]
        packed: List[str] = []

        def counted(job: Job) -> Any:
            packed.append(job.candidates[0].machine_type or "")
            return pack(job)

        with mock.patch.object(step3, "pack", counted):
            after, [result] = Pipeline([CheaperTypes(search_of(before, offers))]).run(before)
        self.assertEqual(packed, ["Standard_D4as_v5", D8S])
        self.assertEqual((result.before, result.after), (438, 292))
        self.assertEqual([each.machine_type for each in after.machines], [D8S])


class SpotTest(unittest.TestCase):
    """A pool on spot today compares its candidates at spot prices."""

    def test_a_spot_pool_is_compared_at_spot_prices(self) -> None:
        """Two D4s v5 at $0.08 on spot cost $117; one D8s v5 holds their pods for $44 at $0.06 on spot, not $219."""
        spot = Pool("node pool spot", "spot", "node pool", False, (Line(TYPE, Capacity.SPOT, 0.08, ""),))
        machines = [machine(name, pool=spot.id, capacity=Capacity.SPOT) for name in ("m1", "m2")]
        pods = [placed(api_pod(f"p-{index}", 1500, 4 * GIB), f"m{1 + index // 2}", spot.id) for index in range(4)]
        before = state([spot], machines, pods)
        cluster = ClusterId(AZURE, "sub-one", REGION, "cluster-one")
        rooms = [(kind(TYPE), offer(TYPE, 0, 4, 16)), (kind(D8S, 8, 32), offer(D8S, 0, 8, 32))]
        found = tuple(Candidate(each, TypeRoom(room.allocatable, {})) for each, room in rooms)
        planned = {spot.id: Plan(found, zones_of(before, spot), Capacity.SPOT, True)}
        keys = {(AZURE, REGION, TYPE, Capacity.SPOT), (AZURE, REGION, D8S, Capacity.SPOT)}
        self.assertEqual(candidate_keys(cluster, planned), keys)
        prices = {
            (AZURE, REGION, TYPE, Capacity.SPOT): 0.08,
            (AZURE, REGION, D8S, Capacity.SPOT): 0.06,
            (AZURE, REGION, TYPE, Capacity.REGULAR): 0.2,
            (AZURE, REGION, D8S, Capacity.REGULAR): 0.3,
        }
        after, [result] = Pipeline([CheaperTypes(searches(cluster, planned, prices))]).run(before)
        self.assertEqual((result.before, result.after), (117, 44))
        lines = [(line.machine_type, line.capacity, line.hourly) for line in after.pools[0].lines]
        self.assertEqual(lines, [(TYPE, Capacity.SPOT, 0.08), (D8S, Capacity.SPOT, 0.06)])


# ---------------------------------------------------------------- speed

# Families of made-up machine types: a name, the GiB each vCPU has, and the price of a vCPU an hour.
FAMILIES = (
    ("c5", 2, 0.0425),
    ("c5a", 2, 0.0385),
    ("c6i", 2, 0.0425),
    ("c6a", 2, 0.0383),
    ("c7i", 2, 0.0446),
    ("m5", 4, 0.048),
    ("m5a", 4, 0.043),
    ("m6i", 4, 0.048),
    ("m6a", 4, 0.0432),
    ("m7i", 4, 0.0504),
    ("r5", 8, 0.063),
    ("r6i", 8, 0.063),
    ("r6a", 8, 0.0567),
)
SIZES = ("large", "xlarge", "2xlarge", "4xlarge", "8xlarge", "12xlarge", "16xlarge", "24xlarge")
VCPUS = (2, 4, 8, 16, 32, 48, 64, 96)
# The requests the large pool's pods ask for, in turn: millicores and MiB.
REQUESTS = ((100, 128), (250, 256), (250, 512), (500, 512), (500, 1024), (100, 256), (750, 1024), (1000, 2048))
LARGE_POOL = "NodePool general"


def type_offer(family: Tuple[str, int, float], size: int, generation: int) -> Offer:
    """Make a made-up type: its vCPUs less a reservation, 90% of its memory, and its price, 3% more each generation."""
    name, ratio, per_vcpu = family
    vcpus = VCPUS[size]
    room = {"cpu": vcpus * 1000 - 70 - 10 * vcpus, "memory": vcpus * ratio * GIB * 9 // 10, "pods": 110}
    hourly = round(vcpus * per_vcpu * (1 + 0.03 * generation), 4)
    return Offer(f"{name}.{SIZES[size]}-g{generation}", "amd64", hourly, room, {})


def large_pool() -> Tuple[ClusterState, Dict[str, Search]]:
    """Make a Karpenter pool of 3,000 pods of 30 workloads on 100 machines of 16 vCPUs in three zones, and 300 types.

    The types are FAMILIES in each of SIZES, in three generations.
    """
    zones = ("zone-a", "zone-b", "zone-c")
    today = type_offer(FAMILIES[5], 3, 0)
    line = Line(today.machine_type, Capacity.REGULAR, today.hourly, "")
    machines = [
        machine(
            f"node-{index:03d}",
            zones[index % 3],
            LARGE_POOL,
            machine_type=today.machine_type,
            allocatable=today.allocatable,
        )
        for index in range(100)
    ]
    pods = []
    for index in range(3000):
        cpu, memory = REQUESTS[index % len(REQUESTS)]
        workload = Workload("Deployment", "shop", f"app-{index % 30:02d}")
        source = api_pod(f"{workload.name}-{index:04d}", cpu, memory * MEBIBYTE, ("ReplicaSet", workload.name))
        pods.append(placed(source, machines[index % 100].name, LARGE_POOL, workload))
    before = state([Pool(LARGE_POOL, "general", "NodePool", False, (line,))], machines, pods)
    offers = [
        type_offer(family, size, generation)
        for generation in range(3)
        for family in FAMILIES
        for size in range(len(SIZES))
    ][:300]
    return before, {LARGE_POOL: Search(tuple(offers), zones_of(before, before.pools[0]), Capacity.REGULAR, False)}


class SpeedTest(unittest.TestCase):
    """The search on a large pool: how many packings it runs, and how long step 3 takes."""

    def test_3000_pods_and_300_types_take_at_most_20_packings_within_60_seconds(self) -> None:
        """Step 3 finds a cheaper way to hold the pool in at most 20 packings, mixes among them, within 60 seconds."""
        before, found = large_pool()
        self.assertEqual((len(before.pods), len(found[LARGE_POOL].offers)), (3000, 300))
        packings: List[Job] = []

        def counted(job: Job) -> Any:
            packings.append(job)
            return pack(job)

        with mock.patch.object(step3, "pack", counted):
            started = time.monotonic()
            after = CheaperTypes(found).apply(before)
            seconds = time.monotonic() - started
        self.assertLessEqual(len(packings), 20)
        self.assertTrue(any(len(job.pods) < len(before.pods) for job in packings))
        self.assertLess(seconds, 60)
        self.assertLess(cost(after), cost(before))
