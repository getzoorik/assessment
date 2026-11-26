"""Made-up clusters for the steps' tests: pods, machines, pools and states built from a few round values.

Requests are in scheduler units: CPU in millicores, memory in bytes. A machine is 4 cores, 16 GiB and 110 pod slots
unless a test says otherwise, and every machine and pod runs Linux on amd64.
"""

from dataclasses import replace
from typing import Any, Mapping, Optional, Sequence, Tuple

from zoorik.kubectl.objects import Container, NodeSelectorTerm, OwnerRef, PersistentVolume, Requirement, Taint
from zoorik.kubectl.objects import Pod as ApiPod
from zoorik.kubectl.usage import MEBIBYTE, ContainerKey, Peak
from zoorik.kubernetes.model import ClusterState, Line, Machine, Pod, Pool, Workload
from zoorik.kubernetes.placement import ARCH_LABEL, HOSTNAME, OS_LABEL, ZONE_LABEL, pod_request, requests_of
from zoorik.pricing.machines import Capacity

GIB = 1024 * MEBIBYTE
TYPE = "Standard_D4s_v5"
APPS = "node pool apps"
NAMESPACE = "shop"


def api_pod(
    name: str,
    cpu: int = 100,
    memory: int = 128 * MEBIBYTE,
    controller: Optional[Tuple[str, str]] = ("ReplicaSet", "web-5d8f"),
    **changes: Any,
) -> ApiPod:
    """Make a running pod in the shop namespace with one container, app, that requests cpu and memory."""
    pod = ApiPod(
        namespace=NAMESPACE,
        name=name,
        controller=None if controller is None else OwnerRef(*controller),
        node=None,
        labels={},
        overhead={},
        requests=None,
        limits=None,
        containers=(Container("app", {"cpu": cpu, "memory": memory}, {}, ()),),
        init_containers=(),
        node_selector={},
        node_affinity=None,
        affinity=(),
        anti_affinity=(),
        tolerations=(),
        spread=(),
        claims=(),
    )
    return replace(pod, **changes)


def placed(
    source: ApiPod,
    node: str,
    pool: str = APPS,
    workload: Optional[Workload] = None,
    **changes: Any,
) -> Pod:
    """Place a pod on a node of pool, its workload its controller's unless given, its request as the scheduler's."""
    pod = Pod(
        source=replace(source, node=node),
        workload=controller_of(source) if workload is None else workload,
        node=node,
        pool=pool,
        request=pod_request(source, requests_of(source), source.requests),
        volumes=(),
        os="linux",
        arch="amd64",
        stays=None,
        raised=False,
        held=False,
    )
    return replace(pod, **changes)


def controller_of(source: ApiPod) -> Workload:
    """Give a pod's controller as its workload, or the pod itself when it has none."""
    owner = source.controller
    if owner is None:
        return Workload("Pod", source.namespace, source.name)
    return Workload(owner.kind, source.namespace, owner.name)


def machine(
    name: str,
    zone: str = "zone-1",
    pool: str = APPS,
    labels: Optional[Mapping[str, str]] = None,
    **changes: Any,
) -> Machine:
    """Make a machine of pool in zone: 4 cores, 16 GiB and 110 pod slots, with hostname, zone, OS and arch labels."""
    own = {HOSTNAME: name, ZONE_LABEL: zone, OS_LABEL: "linux", ARCH_LABEL: "amd64"}
    made = Machine(
        name=name,
        pool=pool,
        machine_type=TYPE,
        capacity=Capacity.REGULAR,
        zone=zone,
        labels={**own, **({} if labels is None else labels)},
        taints=(),
        allocatable={"cpu": 4000, "memory": 16 * GIB, "pods": 110},
        attach={},
    )
    return replace(made, **changes)


def pool(name: str = "apps", hourly: Optional[float] = 0.2, system: bool = False, note: str = "") -> Pool:
    """Make a node pool with one line of the default type at its regular hourly price, or with none and a note."""
    return Pool(f"node pool {name}", name, "node pool", system, (Line(TYPE, Capacity.REGULAR, hourly, note),))


def state(pools: Sequence[Pool], machines: Sequence[Machine], pods: Sequence[Pod]) -> ClusterState:
    """Make a cluster state."""
    return ClusterState(tuple(pools), tuple(machines), tuple(pods))


def peaks(pods: Sequence[Pod], cpu: int, memory: int, container: str = "app") -> Mapping[ContainerKey, Peak]:
    """Give each pod's container one peak: cpu in millicores and memory in bytes."""
    return {ContainerKey(pod.source.namespace, pod.source.name, container): Peak(cpu, memory) for pod in pods}


def local_volume(name: str, node: str) -> PersistentVolume:
    """Make a local volume, bound to one node by its required node affinity."""
    term = NodeSelectorTerm((Requirement(HOSTNAME, "In", (node,)),), ())
    return PersistentVolume(name, "local.csi.example.test", None, (term,), False)


def taint(key: str, effect: str = "NoSchedule", value: Optional[str] = None) -> Taint:
    """Make a node taint."""
    return Taint(key, value, effect)
