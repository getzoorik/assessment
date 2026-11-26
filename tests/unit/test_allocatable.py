"""The allocatable model: reservations checked on a cluster's nodes, and what a type it doesn't run gives pods.

The platforms' reservations, before and after AKS 1.29 and on EKS, the max pods and attach-limit rules, and the misses
that leave a cluster without step 3.
"""

import unittest
from dataclasses import replace
from typing import Mapping, Optional, Sequence, Tuple, Union

from tests.unit.fleet import TYPE, api_pod, machine, placed, pool, state
from zoorik.cloud.azure import AgentPool
from zoorik.cloud.keys import AWS, AZURE
from zoorik.kubectl.objects import Node, PersistentVolume
from zoorik.kubernetes.allocatable import (
    ATTACH_MISS,
    CPU_MISS,
    MEMORY_MISS,
    PODS_MISS,
    UNREAD_VERSION,
    ClusterNodes,
    MachineModel,
    TypeRoom,
    machine_model,
)
from zoorik.kubernetes.catalogue import AZURE_DISK, DATA_DISKS, EBS, SHARED, EniPods, MachineType, Slots
from zoorik.kubernetes.collect import NODE_GROUP
from zoorik.kubernetes.model import Pool

KIB = 1024
D8S = "Standard_D8s_v5"
# A D4s v5 node's capacity as AKS reports it with 110 max pods, and its allocatable by the rule from 1.29: AKS keeps
# min(20 MiB x 110 + 50 MiB, 16 GiB / 4) = 2,250 MiB and evicts at 100 MiB, so 16,393,364 KiB less 2,350 MiB leaves
# 13,986,964 KiB; of the CPU it keeps 60m + 40m + 20m + 20m.
D4S_CAPACITY = {"cpu": 4000, "memory": 16393364 * KIB, "pods": 110}
D4S_ALLOCATABLE = {"cpu": 3860, "memory": 13986964 * KIB, "pods": 110}
# The same node before 1.29: 25% of 4 GiB, 20% of the next 4 and 10% of the next 8 is 2,662 MiB, and it evicts at 750
# MiB, which leaves 12,899,476 KiB; such nodes report 12,880,532 KiB, 0.15% below.
D4S_OLD_MEMORY = 12880532 * KIB
# m6i nodes as EKS reports them. The node images keep 70m and 80m of CPU, and 11 MiB x the VPC CNI's max pods (29 and
# 58) + 255 MiB, evicting at 100 MiB.
Figures = Tuple[Mapping[str, int], Mapping[str, int]]
M6I_LARGE: Figures = (
    {"cpu": 2000, "memory": 7910580 * KIB, "pods": 29},
    {"cpu": 1930, "memory": 7220404 * KIB, "pods": 29},
)
M6I_XLARGE: Figures = (
    {"cpu": 4000, "memory": 15821160 * KIB, "pods": 58},
    {"cpu": 3920, "memory": 14804328 * KIB, "pods": 58},
)
EKS_KUBELET = "v1.30.4-eks-a737599"
GROUP = Pool(f"{NODE_GROUP} general", "general", NODE_GROUP, False, ())
PREMIUM_DISK = PersistentVolume("pv", AZURE_DISK, "Premium_LRS", None, False)


def azure_size(name: str, vcpus: int, memory_gib: int, disks: int) -> MachineType:
    """Make an Azure VM size of vcpus, memory_gib and data-disk slots, in zone 1."""
    return MachineType(
        name=name,
        vcpus=vcpus,
        memory_mib=memory_gib * 1024,
        architecture="amd64",
        burstable=False,
        gpus=0,
        zones=frozenset({"westeurope-1"}),
        regional=False,
        premium_io=True,
        ultra_zones=frozenset(),
        ultra_regional=False,
        attach={AZURE_DISK: Slots(DATA_DISKS, disks)},
        pods=None,
    )


def ec2_type(name: str, vcpus: int, memory_gib: int, interfaces: int, addresses: int) -> MachineType:
    """Make a Nitro EC2 type of vcpus and memory_gib, 28 shared EBS slots, and interfaces that each take addresses."""
    standard = interfaces * (addresses - 1)
    return replace(
        azure_size(name, vcpus, memory_gib, 0),
        zones=frozenset({"eu-west-1a"}),
        attach={EBS: Slots(SHARED, 28)},
        pods=EniPods(standard + 2, standard * 16 + 2),
    )


AZURE_TYPES = {each.name.lower(): each for each in (azure_size(TYPE, 4, 16, 8), azure_size(D8S, 8, 32, 16))}
EC2_TYPES = {
    each.name: each
    for each in (
        ec2_type("m6i.large", 2, 8, 3, 10),
        ec2_type("m6i.xlarge", 4, 16, 4, 15),
        ec2_type("m6i.2xlarge", 8, 32, 4, 15),
    )
}


def aks_cluster(
    version: str = "v1.30.4",
    allocatable: Mapping[str, int] = D4S_ALLOCATABLE,
    second: Optional[Mapping[str, int]] = None,
    volumes: Sequence[PersistentVolume] = (),
) -> ClusterNodes:
    """Give an AKS agent pool apps, maxPods 110, of two D4s v5 nodes at version that report allocatable.

    The second node reports second instead, when given. Each node attaches 8 Azure disks, and its pod uses volumes.
    """
    machines = [machine(name, attach={AZURE_DISK: 8}) for name in ("m1", "m2")]
    pods = [placed(api_pod(f"db-{each.name}"), each.name, volumes=tuple(volumes)) for each in machines]
    nodes = {
        "m1": Node("m1", {}, (), None, D4S_CAPACITY, allocatable, version),
        "m2": Node("m2", {}, (), None, D4S_CAPACITY, allocatable if second is None else second, version),
    }
    agent_pool = AgentPool("apps", "User", None, 110)
    return ClusterNodes(AZURE, state([pool()], machines, pods), nodes, {"apps": agent_pool})


def eks_cluster(nodes: Sequence[Tuple[str, Figures]]) -> ClusterNodes:
    """Give an EKS managed node group of nodes, each a type with its capacity and allocatable.

    Each node attaches 25 EBS volumes, and its pod uses one.
    """
    machines = [
        machine(f"n{index}", "eu-west-1a", GROUP.id, machine_type=kind, attach={EBS: 25})
        for index, (kind, _) in enumerate(nodes)
    ]
    volume = PersistentVolume("pv", EBS, None, None, False)
    pods = [placed(api_pod(f"db-{each.name}"), each.name, GROUP.id, volumes=(volume,)) for each in machines]
    records = {
        each.name: Node(each.name, {}, (), None, capacity, allocatable, EKS_KUBELET)
        for each, (_, (capacity, allocatable)) in zip(machines, nodes)
    }
    return ClusterNodes(AWS, state([GROUP], machines, pods), records, {})


def model_of(cluster: ClusterNodes, types: Mapping[str, MachineType]) -> Union[MachineModel, str]:
    """Give the model of the cluster's pool, or why it can't be trusted."""
    return machine_model(cluster, cluster.state.pools, types)


def room(cluster: ClusterNodes, types: Mapping[str, MachineType], kind: str) -> TypeRoom:
    """Give what a new machine of kind gives the cluster's pool, whose model must be trusted."""
    model = model_of(cluster, types)
    assert isinstance(model, MachineModel), model
    found = model.room(cluster.state.pools[0].id, types[kind.lower()])
    assert found is not None
    return found


class AksReservationTest(unittest.TestCase):
    """AKS's reservation reproduces its nodes before and after 1.29, and carries to a size the cluster doesn't run."""

    def test_nodes_from_1_29_follow_the_new_rule_and_a_new_size_gets_it(self) -> None:
        """A D8s v5 keeps 180m of its CPU, and 2,350 MiB of 32 GiB x the nodes' ratio of capacity to nominal memory.

        That ratio is 16,393,364 KiB to 16 GiB, so its capacity is 33,573,609,472 bytes.
        """
        found = room(aks_cluster(), AZURE_TYPES, D8S)
        self.assertEqual(found.allocatable, {"cpu": 7820, "memory": 31109455872, "pods": 110})

    def test_nodes_before_1_29_follow_the_old_rule_and_a_new_size_gets_it(self) -> None:
        """At 1.28 a D8s v5 keeps 2,662 MiB, 6% of the next 16 GiB (983 MiB) and 750 MiB: 4,395 MiB."""
        old = aks_cluster("v1.28.9", {**D4S_ALLOCATABLE, "memory": D4S_OLD_MEMORY})
        self.assertEqual(room(old, AZURE_TYPES, D8S).allocatable, {"cpu": 7820, "memory": 28965117952, "pods": 110})

    def test_a_node_off_the_rules_is_named(self) -> None:
        """A 1.30 node with the memory of one before 1.29, CPU below the rule's, pods off maxPods, an unread version."""
        cases = {
            "memory": (aks_cluster(second={**D4S_ALLOCATABLE, "memory": D4S_OLD_MEMORY}), MEMORY_MISS),
            "cpu": (aks_cluster(second={**D4S_ALLOCATABLE, "cpu": 3500}), CPU_MISS),
            "pods": (aks_cluster(second={**D4S_ALLOCATABLE, "pods": 30}), PODS_MISS),
            "version": (aks_cluster("unknown"), UNREAD_VERSION),
        }
        for name, (cluster, reason) in cases.items():
            with self.subTest(name):
                self.assertEqual(model_of(cluster, AZURE_TYPES), reason.format(type=TYPE))


class EksReservationTest(unittest.TestCase):
    """The EKS node images' reservation reproduces their nodes, and the VPC CNI's max pods carry to a new type."""

    def test_nodes_follow_the_rule_and_a_new_type_gets_its_vpc_cni_max_pods(self) -> None:
        """An m6i.2xlarge keeps 90m, and 11 MiB x 58 + 255 MiB + 100 MiB of 32 GiB x the nodes' memory ratio."""
        cluster = eks_cluster([("m6i.large", M6I_LARGE), ("m6i.xlarge", M6I_XLARGE)])
        found = room(cluster, EC2_TYPES, "m6i.2xlarge")
        self.assertEqual(found.allocatable, {"cpu": 7910, "memory": 31360499712, "pods": 58})

    def test_prefix_delegation_and_a_set_max_pods_carry_to_a_new_type(self) -> None:
        """Nodes all at 110, above their VPC CNI figures, run prefix delegation; nodes all at 20 run a set figure.

        Nodes that show neither one figure nor the VPC CNI's are named.
        """
        for shown in (110, 20):
            with self.subTest(shown):
                large: Figures = (M6I_LARGE[0], {**M6I_LARGE[1], "pods": shown})
                xlarge: Figures = (M6I_XLARGE[0], {**M6I_XLARGE[1], "pods": shown})
                cluster = eks_cluster([("m6i.large", large), ("m6i.xlarge", xlarge)])
                self.assertEqual(room(cluster, EC2_TYPES, "m6i.2xlarge").allocatable["pods"], shown)
        off: Figures = (M6I_XLARGE[0], {**M6I_XLARGE[1], "pods": 30})
        self.assertEqual(
            model_of(eks_cluster([("m6i.large", M6I_LARGE), ("m6i.xlarge", off)]), EC2_TYPES),
            PODS_MISS.format(type="m6i.large"),
        )


class AttachLimitTest(unittest.TestCase):
    """A CSI driver's attach limit on a new type: its slots less what the pool's nodes hold back, checked on them."""

    def test_a_new_type_gets_its_slots_less_what_the_nodes_hold_back(self) -> None:
        """D4s v5 nodes attach all 8 of their data-disk slots, so a D8s v5 attaches 16; m6i nodes hold back 3 of 28."""
        self.assertEqual(room(aks_cluster(volumes=[PREMIUM_DISK]), AZURE_TYPES, D8S).attach, {AZURE_DISK: 16})
        cluster = eks_cluster([("m6i.large", M6I_LARGE), ("m6i.xlarge", M6I_XLARGE)])
        self.assertEqual(room(cluster, EC2_TYPES, "m6i.2xlarge").attach, {EBS: 25})

    def test_a_node_whose_csinode_count_misses_is_named(self) -> None:
        """Two D4s v5 nodes that attach 8 and 6 disks follow no one rule; nor does one whose CSINode gives no count."""
        for count in (6, None):
            with self.subTest(count):
                cluster = aks_cluster(volumes=[PREMIUM_DISK])
                first, second = cluster.state.machines
                second = replace(second, attach={} if count is None else {AZURE_DISK: count})
                changed = replace(cluster, state=replace(cluster.state, machines=(first, second)))
                self.assertEqual(model_of(changed, AZURE_TYPES), ATTACH_MISS.format(driver=AZURE_DISK, type=TYPE))

    def test_a_driver_no_pod_uses_sets_no_limit(self) -> None:
        """Without a pod that uses an Azure disk, a new size's room has no attach limit."""
        self.assertEqual(room(aks_cluster(), AZURE_TYPES, D8S).attach, {})
