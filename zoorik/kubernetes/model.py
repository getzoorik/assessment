"""The Kubernetes part's values: each cluster as the steps see it, and what the part found on it.

A cluster's state is its pools, the machines of each pool, and the pods on those machines. Each pool's machines come
in lines: machines of one type, bought one way, at one price. Requests are in scheduler units: CPU in millicores,
memory and every other resource in whole units, and one pod slot under "pods".
"""

from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple

from zoorik.cloud.aws import Ec2Instance, NodeGroup
from zoorik.cloud.azure import AgentPool
from zoorik.kubectl.api import Snapshot
from zoorik.kubectl.objects import PersistentVolume, Taint
from zoorik.kubectl.objects import Pod as ApiPod
from zoorik.pricing.machines import Capacity

MEMORY = "memory"
PODS = "pods"
DAEMON_SET = "DaemonSet"

# A resource list, such as a pod's request, in scheduler units.
Resources = Mapping[str, int]


@dataclass(frozen=True)
class ClusterId:
    """A cluster: its cloud, its subscription or account, its region and its name."""

    cloud: str
    account: str
    region: str
    name: str


@dataclass(frozen=True)
class ClusterRead:
    """What the run read of one cluster: its API, and what the cloud's records say of its machines."""

    snapshot: Snapshot
    # The AKS cluster's agent pools, from its record.
    agent_pools: Tuple[AgentPool, ...]
    # The EKS cluster's managed node groups.
    node_groups: Tuple[NodeGroup, ...]
    # The region's EC2 instances, read only when an EKS node carries no provisioner's label.
    instances: Tuple[Ec2Instance, ...]


@dataclass(frozen=True)
class Line:
    """A pool's machines of one type bought one way, and their hourly price, or why they have none."""

    # None when the nodes don't name their machine type.
    machine_type: Optional[str]
    # None when the run can't tell how the machines are bought.
    capacity: Optional[Capacity]
    # The hourly list price, at spot for machines on spot; None when there is none, with the reason in note.
    hourly: Optional[float]
    note: str


@dataclass(frozen=True)
class Pool:
    """A pool of machines, by the provisioner that runs it: its kind and name, and its lines."""

    # Unique in its cluster: its kind and its name.
    id: str
    name: str
    kind: str
    # An AKS pool in System mode, which keeps at least one machine.
    system: bool
    lines: Tuple[Line, ...]

    @property
    def priced(self) -> bool:
        """Tell whether every line of the pool has a price; a pool that doesn't takes no part in the steps."""
        return all(line.hourly is not None for line in self.lines)


@dataclass(frozen=True)
class Machine:
    """A machine of a pool: where it is, its labels and taints, and what it holds."""

    name: str
    pool: str
    machine_type: Optional[str]
    capacity: Optional[Capacity]
    # Its zone label; "" for a machine in no zone.
    zone: str
    # Its own labels, and its type's labels as its line's nodes show them.
    labels: Mapping[str, str]
    taints: Tuple[Taint, ...]
    allocatable: Resources
    # How many volumes each CSI driver can attach to it.
    attach: Mapping[str, int]


@dataclass(frozen=True)
class Workload:
    """A pod's workload: the top controller found above it, or the pod itself when nothing recreates it."""

    kind: str
    namespace: str
    name: str


@dataclass(frozen=True)
class Pod:
    """A pod on one of the cluster's machines, as a state has it: where it is and what it asks for."""

    source: ApiPod
    workload: Workload
    node: str
    # The pool it runs in: a DaemonSet's or static pod stays its pool's overhead, whichever machines a step removes.
    pool: str
    # Its request with one pod slot, as the scheduler counts it.
    request: Resources
    # The volumes its claims are bound to.
    volumes: Tuple[PersistentVolume, ...]
    # The operating system and CPU architecture of its machine today, which its images run on.
    os: Optional[str]
    arch: Optional[str]
    # Why it stays on its node, such as a pod nothing would recreate; None for a pod that may move.
    stays: Optional[str]
    # Smaller pods raised one of its containers above today's request.
    raised: bool
    # Smaller pods would raise its workload, but a machine has no room for that, so it keeps today's requests.
    held: bool

    @property
    def key(self) -> str:
        """Name the pod uniquely in its cluster."""
        return f"{self.source.namespace}/{self.source.name}"

    @property
    def overhead(self) -> bool:
        """Tell whether the pod is machine overhead: a DaemonSet's pod or a static pod, which no step packs."""
        return self.workload.kind == DAEMON_SET or self.source.static


@dataclass(frozen=True)
class ClusterState:
    """A cluster as a step leaves it: its pools, their machines, and the pods on those machines."""

    pools: Tuple[Pool, ...]
    machines: Tuple[Machine, ...]
    pods: Tuple[Pod, ...]

    def counts(self) -> Dict[Tuple[str, Optional[str], Optional[Capacity]], int]:
        """Count the machines of each line, by pool id, machine type and capacity."""
        counts: Dict[Tuple[str, Optional[str], Optional[Capacity]], int] = {}
        for machine in self.machines:
            key = (machine.pool, machine.machine_type, machine.capacity)
            counts[key] = counts.get(key, 0) + 1
        return counts


@dataclass(frozen=True)
class StepResult:
    """What one step left: the cost a month before and after it, in whole dollars, and the page-1 row it counts in."""

    name: str
    row: str
    before: int
    after: int

    @property
    def saving(self) -> int:
        """Give the step's saving a month."""
        return self.before - self.after


@dataclass(frozen=True)
class LineResult:
    """A line's machines and cost a month, today and after the steps; an unpriced line has the reason instead."""

    pool: str
    kind: str
    machine_type: Optional[str]
    # regular or spot; None when the run can't tell.
    capacity: Optional[str]
    hourly: Optional[float]
    note: str
    today: int
    after: int
    # An unpriced line counts in neither total, so its costs are 0.
    cost_today: int
    cost_after: int


@dataclass(frozen=True)
class WorkloadResult:
    """A workload's part of the saving: the CPU and memory it frees, whether it was raised or held, and its share."""

    kind: str
    namespace: str
    name: str
    # Millicores and bytes, over all its pods.
    cpu: int
    memory: int
    raised: bool
    # Raising it needs room a machine doesn't have, so it keeps today's requests.
    held: bool
    share: int


@dataclass(frozen=True)
class ClusterResult:
    """What the part found on one cluster: its lines, the steps' figures, and each workload's share."""

    cluster: ClusterId
    lines: Tuple[LineResult, ...]
    steps: Tuple[StepResult, ...]
    workloads: Tuple[WorkloadResult, ...]
    # The pods nothing would recreate, which stay on their nodes.
    bare_pods: int
    # Fargate and virtual nodes: not machines the run counts.
    not_machines: int
    # Why the cluster's usage wasn't read, so its pods keep today's requests; None when it was.
    usage: Optional[str]

    @property
    def today(self) -> int:
        """Give the cost a month today of the cluster's priced machines."""
        return sum(line.cost_today for line in self.lines)

    @property
    def after(self) -> int:
        """Give the cost a month after the steps."""
        return sum(line.cost_after for line in self.lines)

    @property
    def saving(self) -> int:
        """Give the saving a month: the sum of the steps' savings."""
        return self.today - self.after


@dataclass(frozen=True)
class NotAssessed:
    """A cluster the part names with the reason, such as one that is stopped or can't be read."""

    cloud: str
    account: str
    region: str
    name: str
    reason: str


@dataclass(frozen=True)
class Unread:
    """A subscription or region whose clusters couldn't be listed, and why."""

    cloud: str
    name: str
    reason: str


@dataclass(frozen=True)
class KubernetesResult:
    """The Kubernetes part's result: each assessed cluster, the clusters and scopes left out, and the totals."""

    clusters: Tuple[ClusterResult, ...]
    not_assessed: Tuple[NotAssessed, ...]
    unread: Tuple[Unread, ...]
    # Azure subscriptions' names by id.
    subscriptions: Mapping[str, str]

    @property
    def today(self) -> int:
        """Give the cost a month today of every assessed cluster's priced machines."""
        return sum(cluster.today for cluster in self.clusters)

    @property
    def after(self) -> int:
        """Give the cost a month after the steps."""
        return sum(cluster.after for cluster in self.clusters)

    @property
    def saving(self) -> int:
        """Give the saving a month."""
        return self.today - self.after
