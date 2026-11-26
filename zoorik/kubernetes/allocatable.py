"""What a machine type a cluster doesn't run would give its pods: capacity, less the platform's reservation.

CPU capacity is the type's vCPUs; memory capacity is its nominal memory times the lowest ratio of capacity to nominal
memory among the cluster's nodes; ephemeral storage is the pool's, since the OS disk is a pool setting; pods are the
pool's max pods; each CSI driver's attach limit follows the driver's rule. Allocatable is that capacity less the
platform's reservation for the pool's Kubernetes version and max pods, less the eviction threshold.

The rules are trusted only where they reproduce the cluster's own nodes: each node's allocatable CPU and memory within
1%, its pods exactly, and each CSI driver's attach limit exactly. A cluster where a node misses gets no step 3, with
the reason, which names machine types and never a node or a pool.
"""

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from zoorik.cloud.azure import AgentPool
from zoorik.cloud.keys import AZURE
from zoorik.kubectl.objects import Node
from zoorik.kubectl.quantity import CPU
from zoorik.kubectl.usage import MEBIBYTE
from zoorik.kubernetes.catalogue import MachineType
from zoorik.kubernetes.collect import AGENT_POOL
from zoorik.kubernetes.model import MEMORY, PODS, ClusterState, Machine, Pool, Resources

EPHEMERAL_STORAGE = "ephemeral-storage"
MILLICORES = 1000
# AKS keeps 60m of a machine's first core, 40m of the second, 20m each of the third and fourth, and 10m of each other.
AKS_CPU_TIERS: Tuple[Tuple[Optional[int], int], ...] = ((1, 60), (1, 40), (2, 20), (None, 10))
# From Kubernetes 1.29, AKS keeps 20 MiB a pod plus 50 MiB, at most a quarter of the memory, and evicts at 100 MiB.
AKS_NEW_RULE = (1, 29)
AKS_MIB_PER_POD = 20
AKS_MIB_BASE = 50
AKS_MEMORY_CAP = 4
AKS_EVICTION_MIB = 100
# Before 1.29 it keeps 25% of the first 4 GiB, 20% of the next 4, 10% of the next 8, 6% up to 128 GiB and 2% above,
# and evicts at 750 MiB.
AKS_OLD_MEMORY_TIERS: Tuple[Tuple[Optional[int], int], ...] = (
    (4096, 25),
    (4096, 20),
    (8192, 10),
    (114688, 6),
    (None, 2),
)
AKS_OLD_EVICTION_MIB = 750
# EKS's node images keep 6% of the first core, 1% of the second, 0.5% of the next two and 0.25% of the rest, in
# hundredths of a percent; 11 MiB a pod of the VPC CNI's max pods plus 255 MiB; and evict at 100 MiB.
EKS_CPU_BOUNDS = (0, 1000, 2000, 4000)
EKS_CPU_SHARES = (600, 100, 50, 25)
EKS_CPU_SCALE = 10000
EKS_MIB_PER_POD = 11
EKS_MIB_BASE = 255
EKS_EVICTION_MIB = 100
# A node's allocatable CPU and memory must be within 1% of the rule's.
TOLERANCE_PERCENT = 1
STANDARD = "standard"
PREFIX = "prefix"
VERSION = re.compile(r"v?(\d+)\.(\d+)")

NO_TYPE = "the region's list of machine types has no {type}"
NO_CAPACITY = "its {type} nodes don't give their capacity"
UNREAD_VERSION = "the kubelet version of its {type} nodes is not of a known form"
CPU_MISS = "the allocatable CPU of its {type} nodes doesn't follow the platform's reservation"
MEMORY_MISS = "the allocatable memory of its {type} nodes doesn't follow the platform's reservation"
PODS_MISS = "the pod slots of its {type} nodes don't follow one max pods rule"
ATTACH_MISS = "the {driver} attach limit of its {type} nodes doesn't follow the driver's rule"


class Missed(Exception):
    """A node the rules don't reproduce: why, naming its machine type."""


@dataclass(frozen=True)
class PodsRule:
    """How many pods a pool's machine takes: one figure, the VPC CNI's for its type, or the smaller of the two."""

    fixed: Optional[int]
    # STANDARD or PREFIX: the VPC CNI's figure for the type, without or with prefix delegation.
    eni: Optional[str]

    def figure(self, kind: MachineType) -> Optional[int]:
        """Give the max pods of a machine of the type; None when the rule needs a figure the type lacks."""
        if self.eni is None:
            return self.fixed
        if kind.pods is None:
            return None
        eni = kind.pods.standard if self.eni == STANDARD else kind.pods.prefix
        return eni if self.fixed is None else min(self.fixed, eni)


@dataclass(frozen=True)
class PoolModel:
    """What a new machine of one pool gets: its Kubernetes version, max pods, ephemeral storage and attach limits."""

    version: Tuple[int, int]
    pods: PodsRule
    # The smallest per-node allocatable ephemeral storage of the pool's nodes; None when they don't give it.
    ephemeral: Optional[int]
    # For each CSI driver the pool's pods use with a limit: the slots a node holds back, by how the type counts them.
    held: Mapping[str, Mapping[str, int]]


@dataclass(frozen=True)
class TypeRoom:
    """What a new machine of a type gives a pool's pods: its allocatable, and each CSI driver's attach limit."""

    allocatable: Resources
    attach: Mapping[str, int]


@dataclass(frozen=True)
class ClusterNodes:
    """A cluster as the rules are checked on it: its cloud, its state, its nodes by name and its agent pools by name."""

    cloud: str
    state: ClusterState
    nodes: Mapping[str, Node]
    agent_pools: Mapping[str, AgentPool]


@dataclass(frozen=True)
class MachineModel:
    """The rules a cluster's nodes reproduce, for machine types it doesn't run."""

    cloud: str
    # The lowest ratio of a node's memory capacity to its type's nominal memory.
    memory_ratio: Fraction
    pools: Mapping[str, PoolModel]

    def room(self, pool: str, kind: MachineType) -> Optional[TypeRoom]:
        """Give what a new machine of the type gives the pool's pods; None when a rule can't say, or leaves nothing."""
        model = self.pools[pool]
        pods = model.pods.figure(kind)
        attach = attach_limits(model, kind)
        if pods is None or attach is None:
            return None
        memory = int(kind.memory_mib * MEBIBYTE * self.memory_ratio)
        allocatable = {
            CPU: kind.vcpus * MILLICORES - reserved_cpu(self.cloud, kind.vcpus * MILLICORES),
            MEMORY: memory - reserved_mib(self.cloud, model.version, kind, pods) * MEBIBYTE,
            PODS: pods,
        }
        if any(amount <= 0 for amount in allocatable.values()):
            return None
        if model.ephemeral is not None:
            allocatable[EPHEMERAL_STORAGE] = model.ephemeral
        return TypeRoom(allocatable, attach)


def attach_limits(model: PoolModel, kind: MachineType) -> Optional[Dict[str, int]]:
    """Give each limited CSI driver's attach limit on a new machine of the type; None when its rule doesn't cover it.

    It is the type's slots less what the pool's nodes hold back, at least 1 and at most the type's slots.
    """
    limits: Dict[str, int] = {}
    for driver, held in model.held.items():
        slots = kind.attach.get(driver)
        if slots is None or slots.kind not in held:
            return None
        limits[driver] = min(slots.count, max(1, slots.count - held[slots.kind]))
    return limits


# ---------------------------------------------------------------- the platforms' reservations


def tiered(amount: int, tiers: Sequence[Tuple[Optional[int], int]]) -> int:
    """Total each tier's rate on the part of amount in it; a tier of None size takes the rest."""
    total, left = 0, amount
    for size, rate in tiers:
        part = left if size is None else min(size, left)
        total += part * rate
        left -= part
    return total


def reserved_cpu(cloud: str, millicores: int) -> int:
    """Give the millicores the platform keeps of a machine's CPU for the kubelet and the system."""
    if cloud == AZURE:
        return tiered(millicores // MILLICORES, AKS_CPU_TIERS)
    bounds = (*EKS_CPU_BOUNDS, millicores)
    reserved = 0
    for index, share in enumerate(EKS_CPU_SHARES):
        start, end = bounds[index], bounds[index + 1]
        if millicores > start:
            reserved += (min(millicores, end) - start) * share // EKS_CPU_SCALE
    return reserved


def reserved_mib(cloud: str, version: Tuple[int, int], kind: MachineType, max_pods: int) -> int:
    """Give the MiB the platform keeps of a machine's memory, the eviction threshold included.

    AKS keeps it from the type's nominal memory and the machine's max pods, EKS from the VPC CNI's max pods.
    """
    if cloud == AZURE:
        if version >= AKS_NEW_RULE:
            kept = min(AKS_MIB_PER_POD * max_pods + AKS_MIB_BASE, kind.memory_mib // AKS_MEMORY_CAP)
            return kept + AKS_EVICTION_MIB
        return tiered(kind.memory_mib, AKS_OLD_MEMORY_TIERS) // 100 + AKS_OLD_EVICTION_MIB
    if kind.pods is None:
        # Only an EC2 instance type has the VPC CNI's max pods that EKS reserves for.
        raise TypeError(f"{kind.name} is not an EC2 instance type")
    # EKS reserves for the VPC CNI's max pods, whatever max pods the node runs with.
    return EKS_MIB_PER_POD * kind.pods.standard + EKS_MIB_BASE + EKS_EVICTION_MIB


def near(rule: int, observed: int) -> bool:
    """Tell whether a node's figure is within 1% of the rule's."""
    return abs(rule - observed) * 100 <= TOLERANCE_PERCENT * observed


def kube_version(text: str) -> Optional[Tuple[int, int]]:
    """Read a kubelet version, such as v1.30.4, as its major and minor numbers; None when it is of another form."""
    found = VERSION.match(text)
    return None if found is None else (int(found.group(1)), int(found.group(2)))


# ---------------------------------------------------------------- the rules, checked on the cluster's nodes


@dataclass(frozen=True)
class Observed:
    """A node of a pool the rules are checked on: its machine, its record and its type."""

    machine: Machine
    node: Node
    kind: MachineType

    @property
    def type_name(self) -> str:
        """Name its machine type."""
        return self.kind.name


def machine_model(
    cluster: ClusterNodes, pools: Sequence[Pool], types: Mapping[str, MachineType]
) -> Union[MachineModel, str]:
    """Check the rules on the pools' nodes, and give the model of their machine types, or why it can't be trusted."""
    try:
        observed = {pool.id: observe(cluster, pool, types) for pool in pools}
        models = {pool.id: pool_model(cluster, pool, observed[pool.id]) for pool in pools}
    except Missed as missed:
        return str(missed)
    nodes = [each for group in observed.values() for each in group]
    ratios = [Fraction(each.node.capacity[MEMORY], each.kind.memory_mib * MEBIBYTE) for each in nodes]
    return MachineModel(cluster.cloud, min(ratios), models)


def observe(cluster: ClusterNodes, pool: Pool, types: Mapping[str, MachineType]) -> List[Observed]:
    """Give a pool's nodes with their types; Missed when the region's list lacks a type or a node its capacity."""
    found: List[Observed] = []
    for machine in cluster.state.machines:
        if machine.pool != pool.id:
            continue
        name = "" if machine.machine_type is None else machine.machine_type
        kind = types.get(name.lower())
        if kind is None:
            raise Missed(NO_TYPE.format(type=name))
        node = cluster.nodes[machine.name]
        if any(key not in node.capacity or key not in node.allocatable for key in (CPU, MEMORY, PODS)):
            raise Missed(NO_CAPACITY.format(type=kind.name))
        found.append(Observed(machine, node, kind))
    return found


def pool_model(cluster: ClusterNodes, pool: Pool, observed: Sequence[Observed]) -> PoolModel:
    """Check the rules on one pool's nodes, and give what a new machine of the pool gets; Missed when one misses."""
    versions = [version_of(each) for each in observed]
    rule = pods_rule(cluster, pool, observed)
    for each, version in zip(observed, versions):
        check_node(cluster.cloud, each, version, rule)
    storage = [each.node.allocatable.get(EPHEMERAL_STORAGE) for each in observed]
    known = [found for found in storage if found is not None]
    return PoolModel(
        version=max(versions),
        pods=rule,
        # The pool's new machines get its smallest node's ephemeral storage, when every node gives it.
        ephemeral=min(known) if len(known) == len(storage) else None,
        held=held_slots(observed, cluster.state),
    )


def version_of(each: Observed) -> Tuple[int, int]:
    """Give a node's Kubernetes version; Missed when its kubelet version is of another form."""
    version = kube_version(each.node.kubelet_version)
    if version is None:
        raise Missed(UNREAD_VERSION.format(type=each.type_name))
    return version


def check_node(cloud: str, each: Observed, version: Tuple[int, int], rule: PodsRule) -> None:
    """Check that the rules reproduce a node's allocatable CPU, memory and pods; Missed when one doesn't."""
    capacity, allocatable = each.node.capacity, each.node.allocatable
    pods = allocatable[PODS]
    if rule.figure(each.kind) != pods:
        raise Missed(PODS_MISS.format(type=each.type_name))
    if not near(capacity[CPU] - reserved_cpu(cloud, capacity[CPU]), allocatable[CPU]):
        raise Missed(CPU_MISS.format(type=each.type_name))
    memory = capacity[MEMORY] - reserved_mib(cloud, version, each.kind, pods) * MEBIBYTE
    if not near(memory, allocatable[MEMORY]):
        raise Missed(MEMORY_MISS.format(type=each.type_name))


def pods_rule(cluster: ClusterNodes, pool: Pool, observed: Sequence[Observed]) -> PodsRule:
    """Decide a pool's max pods rule from its record or its nodes; Missed when its nodes follow none.

    An AKS agent pool's is its maxPods. On EKS, nodes that each show the VPC CNI's figure for their type follow it;
    nodes that all show one figure follow that figure, within the VPC CNI's figure with prefix delegation when it is
    above some node's figure without, else within that one. Any other pool's nodes must all show one figure.
    """
    figures = [each.node.allocatable[PODS] for each in observed]
    cloud = cluster.cloud
    agent_pool = cluster.agent_pools.get(pool.name) if pool.kind == AGENT_POOL else None
    if cloud == AZURE and agent_pool is not None:
        return PodsRule(agent_pool.max_pods, None)
    if cloud != AZURE and all(
        each.kind.pods is not None and figure == each.kind.pods.standard for each, figure in zip(observed, figures)
    ):
        return PodsRule(None, STANDARD)
    if len(set(figures)) != 1:
        raise Missed(PODS_MISS.format(type=observed[0].type_name))
    if cloud == AZURE:
        return PodsRule(figures[0], None)
    beyond = any(each.kind.pods is not None and figures[0] > each.kind.pods.standard for each in observed)
    return PodsRule(figures[0], PREFIX if beyond else STANDARD)


def held_slots(observed: Sequence[Observed], state: ClusterState) -> Dict[str, Dict[str, int]]:
    """Give, for each CSI driver the pool's pods use with a limit, the slots its nodes hold back, by how they count.

    It is the type's slots less the driver's count on the node, one value for each way of counting; Missed when the
    nodes give no one value, or a type has no rule for the driver.
    """
    names = {each.machine.name for each in observed}
    drivers = sorted(
        {
            volume.driver
            for pod in state.pods
            if pod.node in names
            for volume in pod.volumes
            if volume.driver is not None
        }
    )
    held: Dict[str, Dict[str, int]] = {}
    for driver in drivers:
        counts = [each.machine.attach.get(driver) for each in observed]
        if all(count is None for count in counts):
            # The pool's nodes give no limit for the driver, so neither do its new machines.
            continue
        held[driver] = held_for(driver, observed, counts)
    return held


def held_for(driver: str, observed: Sequence[Observed], counts: Sequence[Optional[int]]) -> Dict[str, int]:
    """Give the slots a pool's nodes hold back from one driver, by how their types count them; Missed if they differ."""
    found: Dict[str, Set[int]] = {}
    for each, count in zip(observed, counts):
        slots = each.kind.attach.get(driver)
        if slots is None or count is None:
            raise Missed(ATTACH_MISS.format(driver=driver, type=each.type_name))
        found.setdefault(slots.kind, set()).add(slots.count - count)
    for each in observed:
        slots = each.kind.attach[driver]
        if len(found[slots.kind]) != 1:
            raise Missed(ATTACH_MISS.format(driver=driver, type=each.type_name))
    return {kind: values.pop() for kind, values in found.items()}
