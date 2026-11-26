"""One cluster as the steps see it: its pools by what provisions their machines, and the pods on those machines.

A node's pool is its Karpenter NodePool, its AKS node pool or its EKS managed node group; an EKS node none of them
label belongs to its Auto Scaling group. Fargate and virtual nodes are not machines: they are counted, and their pods
left out. A machine's type, zone, CPU architecture and OS come from its labels, and how it is bought from its
provisioner. A pod's workload is the top controller found above it.
"""

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from zoorik.cloud.keys import AWS
from zoorik.kubectl.objects import Node, OwnerRef, PersistentVolume
from zoorik.kubectl.objects import Pod as ApiPod
from zoorik.kubernetes.model import ClusterRead, ClusterState, Line, Machine, Pod, Pool, Workload
from zoorik.kubernetes.placement import (
    ARCH_LABEL,
    OS_LABEL,
    ZONE_LABEL,
    bound_to_one_node,
    machine_labels,
    pod_request,
    requests_of,
    selects_namespaces_by_label,
    type_labels,
)
from zoorik.pricing.machines import Capacity

TYPE_LABEL = "node.kubernetes.io/instance-type"
NODE_POOL_LABEL = "karpenter.sh/nodepool"
CAPACITY_LABEL = "karpenter.sh/capacity-type"
AGENT_POOL_LABEL = "kubernetes.azure.com/agentpool"
NODE_GROUP_LABEL = "eks.amazonaws.com/nodegroup"
PROVISIONER_LABELS = (NODE_POOL_LABEL, AGENT_POOL_LABEL, NODE_GROUP_LABEL)
# Fargate nodes and virtual nodes run each pod on capacity of their own, which no pool's machines hold.
NOT_MACHINES = (("eks.amazonaws.com/compute-type", "fargate"), ("type", "virtual-kubelet"))
# EC2 tags an Auto Scaling group's instances with its name.
GROUP_TAG = "aws:autoscaling:groupName"
LINUX = "linux"
SYSTEM_MODE = "System"
SPOT_PRIORITY = "Spot"
NODE_POOL_CAPACITIES = {"spot": Capacity.SPOT, "on-demand": Capacity.REGULAR}
NODE_GROUP_CAPACITIES = {"SPOT": Capacity.SPOT, "ON_DEMAND": Capacity.REGULAR}
# EC2 leaves InstanceLifecycle out for an on-demand instance.
LIFECYCLES = {None: Capacity.REGULAR, "spot": Capacity.SPOT}
# A pool's kind names what provisions its machines.
NODE_POOL = "NodePool"
AGENT_POOL = "node pool"
NODE_GROUP = "node group"
SCALING_GROUP = "Auto Scaling group"
LONE_NODE = "node"
# A pod no controller would recreate is its own workload.
BARE = "Pod"
TOP_KINDS = ("ReplicaSet", "Job")
# Why a pod stays on its node.
NO_CONTROLLER = "nothing would recreate it"
LOCAL_VOLUME = "its volume is bound to its node"
VOLUME_UNKNOWN = "its volume claim is not bound to a volume the run read"
NAMESPACES_BY_LABEL = "its required affinity selects namespaces by label"


@dataclass(frozen=True)
class Membership:
    """The pool a node is in, how its machine is bought, and why it has no price when it can't have one."""

    kind: str
    name: str
    system: bool
    # None when the run can't tell how the machine is bought.
    capacity: Optional[Capacity]
    note: str

    @property
    def pool(self) -> str:
        """Name the pool uniquely in its cluster: its kind and its name."""
        return f"{self.kind} {self.name}"


@dataclass(frozen=True)
class Collected:
    """A cluster as the steps see it, and how many of its nodes are not machines."""

    state: ClusterState
    not_machines: int


def not_machine(node: Node) -> bool:
    """Tell whether a node is a Fargate or virtual node, which is not a machine."""
    return any(node.labels.get(key) == value for key, value in NOT_MACHINES)


def unmarked(node: Node) -> bool:
    """Tell whether a machine carries no provisioner's label, so only its EC2 instance can name its pool."""
    return not not_machine(node) and not any(label in node.labels for label in PROVISIONER_LABELS)


def instance_id(node: Node) -> Optional[str]:
    """Give the EC2 instance id at the end of a node's provider id, such as aws:///eu-west-1a/i-0abc."""
    return None if node.provider_id is None else node.provider_id.rsplit("/", 1)[-1]


def priced_or_why(kind: str, value: Optional[str], capacity: Optional[Capacity]) -> str:
    """Say why a machine bought some way has no price; "" when its capacity is known."""
    if capacity is not None:
        return ""
    return f"its {kind} doesn't say how it is bought" if value is None else f"{value} capacity is not priced"


class Memberships:
    """Finds each node's pool from its labels and the cloud's records."""

    def __init__(self, cloud: str, read: ClusterRead) -> None:
        """Look nodes up in read's records, by the rules of cloud."""
        self._cloud = cloud
        self._agent_pools = {pool.name: pool for pool in read.agent_pools}
        self._node_groups = {group.name: group for group in read.node_groups}
        self._instances = {instance.id: instance for instance in read.instances}

    def of(self, node: Node) -> Membership:
        """Give a machine's pool, by the first provisioner whose label it carries."""
        labels = node.labels
        if NODE_POOL_LABEL in labels:
            return node_pool(labels)
        if AGENT_POOL_LABEL in labels:
            return self._agent_pool(labels[AGENT_POOL_LABEL])
        if NODE_GROUP_LABEL in labels:
            return self._node_group(labels[NODE_GROUP_LABEL])
        if self._cloud == AWS:
            return self._instance(node)
        return Membership(LONE_NODE, node.name, False, None, "its node names no node pool")

    def _agent_pool(self, name: str) -> Membership:
        """Give an AKS node's node pool, from the cluster's record."""
        pool = self._agent_pools.get(name)
        if pool is None:
            return Membership(AGENT_POOL, name, False, None, "its node pool is not in the cluster's record")
        capacity = Capacity.SPOT if pool.priority == SPOT_PRIORITY else Capacity.REGULAR
        return Membership(AGENT_POOL, name, pool.mode == SYSTEM_MODE, capacity, "")

    def _node_group(self, name: str) -> Membership:
        """Give an EKS node's managed node group, from its record."""
        group = self._node_groups.get(name)
        if group is None:
            return Membership(NODE_GROUP, name, False, None, "its node group is not in the cluster's record")
        capacity = NODE_GROUP_CAPACITIES.get(group.capacity_type)
        return Membership(NODE_GROUP, name, False, capacity, priced_or_why("node group", group.capacity_type, capacity))

    def _instance(self, node: Node) -> Membership:
        """Give a self-managed EKS node's Auto Scaling group, or the node alone, from its EC2 instance."""
        key = instance_id(node)
        instance = None if key is None else self._instances.get(key)
        if instance is None:
            return Membership(LONE_NODE, node.name, False, None, "its EC2 instance was not found")
        capacity = LIFECYCLES.get(instance.lifecycle)
        note = priced_or_why("instance", instance.lifecycle, capacity)
        group = None if instance.tags is None else instance.tags.get(GROUP_TAG)
        if group is None:
            return Membership(LONE_NODE, node.name, False, capacity, note)
        return Membership(SCALING_GROUP, group, False, capacity, note)


def node_pool(labels: Mapping[str, str]) -> Membership:
    """Give a Karpenter node's NodePool, bought as its capacity-type label says."""
    value = labels.get(CAPACITY_LABEL)
    capacity = None if value is None else NODE_POOL_CAPACITIES.get(value)
    return Membership(NODE_POOL, labels[NODE_POOL_LABEL], False, capacity, priced_or_why("node", value, capacity))


def line_note(membership: Membership, machine_type: Optional[str], oses: Sequence[Optional[str]]) -> str:
    """Say why a line's machines have no price, or "" when the price list may have one."""
    if membership.note:
        return membership.note
    if machine_type is None:
        return "its nodes don't name their machine type"
    if any(os != LINUX for os in oses):
        return "only Linux machines are priced"
    return ""


# A line's key: its pool, machine type and capacity.
LineKey = Tuple[str, Optional[str], Optional[Capacity]]


def line_order(key: LineKey) -> Tuple[str, str, str]:
    """Order lines by pool, machine type and capacity."""
    pool, machine_type, capacity = key
    return (pool, "" if machine_type is None else machine_type, "" if capacity is None else capacity.value)


def machines_and_pools(cloud: str, read: ClusterRead) -> Tuple[Tuple[Machine, ...], Tuple[Pool, ...]]:
    """Give the cluster's machines and their pools, each pool's lines by machine type and capacity."""
    memberships = Memberships(cloud, read)
    lines: Dict[LineKey, List[Tuple[Node, Membership]]] = {}
    for node in read.snapshot.nodes:
        if not not_machine(node):
            membership = memberships.of(node)
            key = (membership.pool, node.labels.get(TYPE_LABEL), membership.capacity)
            lines.setdefault(key, []).append((node, membership))
    attach = {
        csi.name: {driver: count for driver, count in csi.drivers.items() if count is not None}
        for csi in read.snapshot.csi_nodes
    }
    machines: List[Machine] = []
    pools: Dict[str, Tuple[Membership, List[Line]]] = {}
    for key in sorted(lines, key=line_order):
        pool, machine_type, capacity = key
        members = lines[key]
        membership = members[0][1]
        note = line_note(membership, machine_type, [node.labels.get(OS_LABEL) for node, _ in members])
        pools.setdefault(pool, (membership, []))[1].append(Line(machine_type, capacity, None, note))
        shared = type_labels([node.labels for node, _ in members])
        machines.extend(
            Machine(
                name=node.name,
                pool=pool,
                machine_type=machine_type,
                capacity=capacity,
                zone=node.labels.get(ZONE_LABEL, ""),
                labels=machine_labels(node.labels, shared),
                taints=node.taints,
                allocatable=node.allocatable,
                attach=attach.get(node.name, {}),
            )
            for node, _ in members
        )
    return (
        tuple(sorted(machines, key=lambda machine: machine.name)),
        tuple(
            Pool(pool, membership.name, membership.kind, membership.system, tuple(pool_lines))
            for pool, (membership, pool_lines) in sorted(pools.items())
        ),
    )


def workload_of(pod: ApiPod, owners: Mapping[Tuple[str, str, str], OwnerRef]) -> Workload:
    """Give the pod's workload: the controller above its ReplicaSet or Job, else its own controller, else itself."""
    controller = pod.controller
    if controller is None or pod.static:
        return Workload(BARE, pod.namespace, pod.name)
    if controller.kind in TOP_KINDS:
        above = owners.get((controller.kind, pod.namespace, controller.name))
        if above is not None:
            return Workload(above.kind, pod.namespace, above.name)
    return Workload(controller.kind, pod.namespace, controller.name)


def volumes_of(
    pod: ApiPod, claims: Mapping[Tuple[str, str], Optional[str]], volumes: Mapping[str, PersistentVolume]
) -> Optional[Tuple[PersistentVolume, ...]]:
    """Give the volumes the pod's claims are bound to; None when one isn't bound to a volume the run read."""
    found: List[PersistentVolume] = []
    for claim in pod.claims:
        name = claims.get((pod.namespace, claim))
        volume = None if name is None else volumes.get(name)
        if volume is None:
            return None
        found.append(volume)
    return tuple(found)


def stays(pod: ApiPod, volumes: Optional[Sequence[PersistentVolume]]) -> Optional[str]:
    """Say why a pod stays on its node; None for a pod that may move."""
    if pod.controller is None:
        return NO_CONTROLLER
    if volumes is None:
        return VOLUME_UNKNOWN
    if any(bound_to_one_node(volume) for volume in volumes):
        return LOCAL_VOLUME
    if selects_namespaces_by_label(pod):
        return NAMESPACES_BY_LABEL
    return None


def collect(cloud: str, read: ClusterRead) -> Collected:
    """Give the cluster's pools, machines and placed pods as the steps see them."""
    snapshot = read.snapshot
    machines, pools = machines_and_pools(cloud, read)
    by_name = {machine.name: machine for machine in machines}
    owners = {(owned.kind, owned.namespace, owned.name): owned.controller for owned in snapshot.owners}
    claims = {(claim.namespace, claim.name): claim.volume_name for claim in snapshot.claims}
    volumes = {volume.name: volume for volume in snapshot.volumes}
    pods: List[Pod] = []
    for source in snapshot.pods:
        machine = None if source.node is None else by_name.get(source.node)
        if machine is None:
            # A pod not placed yet, or on a Fargate or virtual node, holds no machine's room.
            continue
        bound = volumes_of(source, claims, volumes)
        pods.append(
            Pod(
                source=source,
                workload=workload_of(source, owners),
                node=machine.name,
                pool=machine.pool,
                request=pod_request(source, requests_of(source), source.requests),
                volumes=() if bound is None else bound,
                os=machine.labels.get(OS_LABEL),
                arch=machine.labels.get(ARCH_LABEL),
                stays=stays(source, bound),
                raised=False,
                held=False,
            )
        )
    not_machines = sum(1 for node in snapshot.nodes if not_machine(node))
    return Collected(ClusterState(pools, machines, tuple(sorted(pods, key=lambda pod: pod.key))), not_machines)
