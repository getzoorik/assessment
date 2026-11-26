"""One cluster as the steps see it: pools by node kind, machines from labels, workloads and pods that stay put."""

import unittest
from dataclasses import replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from tests.unit.fleet import api_pod
from zoorik.cloud.aws import Ec2Instance, NodeGroup
from zoorik.cloud.azure import AgentPool
from zoorik.cloud.keys import AWS, AZURE
from zoorik.kubectl.api import Snapshot
from zoorik.kubectl.objects import (
    AffinityTerm,
    CsiNode,
    LabelSelector,
    Node,
    NodeSelectorTerm,
    Owned,
    OwnerRef,
    PersistentVolume,
    PersistentVolumeClaim,
    Requirement,
)
from zoorik.kubectl.objects import Pod as ApiPod
from zoorik.kubectl.usage import Usage
from zoorik.kubernetes.collect import (
    LOCAL_VOLUME,
    NAMESPACES_BY_LABEL,
    NO_CONTROLLER,
    VOLUME_UNKNOWN,
    Collected,
    collect,
    unmarked,
)
from zoorik.kubernetes.model import ClusterRead, Workload
from zoorik.kubernetes.placement import ARCH_LABEL, HOSTNAME, OS_LABEL, ZONE_LABEL
from zoorik.pricing.machines import Capacity

TYPE_LABEL = "node.kubernetes.io/instance-type"


def node(name: str, labels: Mapping[str, str], provider_id: Optional[str] = None) -> Node:
    """Make a Linux amd64 node of type m5.xlarge in zone-1, with labels."""
    own = {HOSTNAME: name, OS_LABEL: "linux", ARCH_LABEL: "amd64", ZONE_LABEL: "zone-1", TYPE_LABEL: "m5.xlarge"}
    resources = {"cpu": 4000, "memory": 16 * 2**30, "pods": 110}
    return Node(name, {**own, **labels}, (), provider_id, resources, resources, "v1.30.4")


def snapshot(nodes: Sequence[Node], pods: Sequence[ApiPod] = (), **kinds: Any) -> Snapshot:
    """Make a snapshot of nodes and pods, with any other kind given by name."""
    made = Snapshot(tuple(nodes), tuple(pods), (), (), (), (), (), Usage({}, None))
    return replace(made, **kinds)


def agent_pool(name: str, mode: str = "User", priority: Optional[str] = None) -> AgentPool:
    """Make an AKS agent pool's record."""
    return AgentPool(name, mode, priority, 110)


def instance(number: int, tags: Optional[Dict[str, str]], lifecycle: Optional[str]) -> Ec2Instance:
    """Make an EC2 instance i-0<number>."""
    return Ec2Instance(f"i-0{number}", "m5.xlarge", "running", None, tags, lifecycle, "/dev/xvda", (), 1)


def provider(number: int) -> str:
    """Give the provider id of the node on instance i-0<number>."""
    return f"aws:///zone-1/i-0{number}"


def eks_read(nodes: Sequence[Node], pods: Sequence[ApiPod] = ()) -> ClusterRead:
    """Make what the run read of an EKS cluster with two managed node groups and two self-managed instances."""
    groups = (NodeGroup("ng-od", "ON_DEMAND"), NodeGroup("ng-block", "CAPACITY_BLOCK"), NodeGroup("ng-spot", "SPOT"))
    instances = (instance(1, {"aws:autoscaling:groupName": "asg-one"}, "spot"), instance(2, None, None))
    return ClusterRead(snapshot(nodes, pods), (), groups, instances)


def lines(collected: Collected) -> List[Tuple[str, Optional[str], Optional[Capacity], str]]:
    """Give each line: its pool, machine type, capacity and note."""
    return [
        (pool.id, line.machine_type, line.capacity, line.note) for pool in collected.state.pools for line in pool.lines
    ]


class PoolsTest(unittest.TestCase):
    """Each node's pool comes from the provisioner that labels it, and how its machine is bought from its record."""

    def test_aks_nodes_are_in_their_agent_pools(self) -> None:
        """The system pool is in System mode; the spot pool's machines are on spot."""
        nodes = [
            node("aks-system-0", {"kubernetes.azure.com/agentpool": "system"}),
            node("aks-spot-0", {"kubernetes.azure.com/agentpool": "spot"}),
            node("aks-gone-0", {"kubernetes.azure.com/agentpool": "gone"}),
        ]
        records = (agent_pool("system", "System"), agent_pool("spot", priority="Spot"))
        collected = collect(AZURE, ClusterRead(snapshot(nodes), records, (), ()))
        self.assertEqual(
            [(pool.id, pool.kind, pool.system) for pool in collected.state.pools],
            [
                ("node pool gone", "node pool", False),
                ("node pool spot", "node pool", False),
                ("node pool system", "node pool", True),
            ],
        )
        self.assertEqual(
            lines(collected),
            [
                ("node pool gone", "m5.xlarge", None, "its node pool is not in the cluster's record"),
                ("node pool spot", "m5.xlarge", Capacity.SPOT, ""),
                ("node pool system", "m5.xlarge", Capacity.REGULAR, ""),
            ],
        )

    def test_eks_nodes_are_in_node_groups_node_pools_or_auto_scaling_groups(self) -> None:
        """Managed groups and NodePools come from their labels; an unlabelled node from its EC2 instance's group."""
        nodes = [
            node("od-0", {"eks.amazonaws.com/nodegroup": "ng-od"}),
            node("spot-0", {"eks.amazonaws.com/nodegroup": "ng-spot"}),
            node("block-0", {"eks.amazonaws.com/nodegroup": "ng-block"}),
            node("karp-0", {"karpenter.sh/nodepool": "default", "karpenter.sh/capacity-type": "spot"}),
            node("karp-1", {"karpenter.sh/nodepool": "default"}),
            node("self-1", {}, provider(1)),
            node("self-2", {}, provider(2)),
            node("self-3", {}, provider(3)),
            node("fargate-ip-10-0-0-1", {"eks.amazonaws.com/compute-type": "fargate"}),
        ]
        collected = collect(AWS, eks_read(nodes))
        self.assertEqual(
            lines(collected),
            [
                ("Auto Scaling group asg-one", "m5.xlarge", Capacity.SPOT, ""),
                ("NodePool default", "m5.xlarge", None, "its node doesn't say how it is bought"),
                ("NodePool default", "m5.xlarge", Capacity.SPOT, ""),
                ("node group ng-block", "m5.xlarge", None, "CAPACITY_BLOCK capacity is not priced"),
                ("node group ng-od", "m5.xlarge", Capacity.REGULAR, ""),
                ("node group ng-spot", "m5.xlarge", Capacity.SPOT, ""),
                ("node self-2", "m5.xlarge", Capacity.REGULAR, ""),
                ("node self-3", "m5.xlarge", None, "its EC2 instance was not found"),
            ],
        )
        self.assertEqual((len(collected.state.machines), collected.not_machines), (8, 1))
        self.assertEqual([each.name for each in nodes if unmarked(each)], ["self-1", "self-2", "self-3"])

    def test_a_line_names_why_it_has_no_price(self) -> None:
        """A Windows node is not priced, nor is a node that doesn't name its machine type."""
        windows = node("aks-win-0", {"kubernetes.azure.com/agentpool": "win", OS_LABEL: "windows"})
        untyped = replace(
            node("aks-odd-0", {"kubernetes.azure.com/agentpool": "odd"}),
            labels={HOSTNAME: "aks-odd-0", "kubernetes.azure.com/agentpool": "odd", OS_LABEL: "linux"},
        )
        collected = collect(
            AZURE, ClusterRead(snapshot([windows, untyped]), (agent_pool("win"), agent_pool("odd")), (), ())
        )
        self.assertEqual(
            [note for _, _, _, note in lines(collected)],
            ["its nodes don't name their machine type", "only Linux machines are priced"],
        )
        self.assertFalse(any(pool.priced for pool in collected.state.pools))

    def test_a_machine_has_its_zone_type_labels_and_attach_limits(self) -> None:
        """The zone and labels come from the node; the attach limits from its CSINode's drivers that give one."""
        labelled = node(
            "aks-db-0",
            {
                "kubernetes.azure.com/agentpool": "db",
                ZONE_LABEL: "zone-2",
                "kubernetes.azure.com/accelerator": "nvidia",
            },
        )
        drivers = CsiNode("aks-db-0", {"disk.csi.azure.com": 8, "file.csi.azure.com": None})
        collected = collect(AZURE, ClusterRead(snapshot([labelled], csi_nodes=(drivers,)), (agent_pool("db"),), (), ()))
        made = collected.state.machines[0]
        self.assertEqual(
            (made.zone, made.attach, made.labels["kubernetes.azure.com/accelerator"]),
            ("zone-2", {"disk.csi.azure.com": 8}, "nvidia"),
        )


class PodsTest(unittest.TestCase):
    """Each pod's workload, and why some stay on their node."""

    def collected(self, pods: Sequence[ApiPod], **kinds: Any) -> Collected:
        """Collect pods on one AKS node, with any other kind given."""
        nodes = [node("aks-apps-0", {"kubernetes.azure.com/agentpool": "apps"})]
        return collect(AZURE, ClusterRead(snapshot(nodes, pods, **kinds), (agent_pool("apps"),), (), ()))

    def test_a_pods_workload_is_its_top_controller(self) -> None:
        """A ReplicaSet leads to its Deployment and a Job to its CronJob; a pod with no controller is its own."""
        pods = [
            api_pod("web-5d8f-a1", controller=("ReplicaSet", "web-5d8f"), node="aks-apps-0"),
            api_pod("old-1", controller=("ReplicaSet", "old"), node="aks-apps-0"),
            api_pod("report-1", controller=("Job", "report-281"), node="aks-apps-0"),
            api_pod("db-0", controller=("StatefulSet", "db"), node="aks-apps-0"),
            api_pod("debug", controller=None, node="aks-apps-0"),
            api_pod("proxy-aks-apps-0", controller=("Node", "aks-apps-0"), node="aks-apps-0"),
        ]
        owners = (
            Owned("ReplicaSet", "shop", "web-5d8f", OwnerRef("Deployment", "web")),
            Owned("Job", "shop", "report-281", OwnerRef("CronJob", "report")),
        )
        collected = self.collected(pods, owners=owners)
        workloads = {pod.source.name: (pod.workload, pod.stays, pod.overhead) for pod in collected.state.pods}
        self.assertEqual(
            workloads,
            {
                "web-5d8f-a1": (Workload("Deployment", "shop", "web"), None, False),
                "old-1": (Workload("ReplicaSet", "shop", "old"), None, False),
                "report-1": (Workload("CronJob", "shop", "report"), None, False),
                "db-0": (Workload("StatefulSet", "shop", "db"), None, False),
                "debug": (Workload("Pod", "shop", "debug"), NO_CONTROLLER, False),
                "proxy-aks-apps-0": (Workload("Pod", "shop", "proxy-aks-apps-0"), None, True),
            },
        )

    def test_pods_whose_volumes_or_namespace_selectors_keep_them_on_their_node_stay(self) -> None:
        """A volume bound to the node, a claim bound to no volume read, or a namespace selector keeps the pod there."""
        local = PersistentVolume(
            "pv-local", None, None, (NodeSelectorTerm((Requirement(HOSTNAME, "In", ("aks-apps-0",)),), ()),)
        )
        zonal = PersistentVolume("pv-zonal", "disk.csi.azure.com", "Premium_LRS", None)
        by_label = AffinityTerm(
            LabelSelector({"app": "web"}, ()), (), LabelSelector({"team": "one"}, ()), HOSTNAME, (), ()
        )
        pods = [
            api_pod("db-0", claims=("data-db-0",), node="aks-apps-0"),
            api_pod("db-1", claims=("data-db-1",), node="aks-apps-0"),
            api_pod("cache-0", claims=("data-cache-0",), node="aks-apps-0"),
            api_pod("app-1", affinity=(by_label,), node="aks-apps-0"),
        ]
        claims = (
            PersistentVolumeClaim("shop", "data-db-0", "pv-local"),
            PersistentVolumeClaim("shop", "data-db-1", None),
            PersistentVolumeClaim("shop", "data-cache-0", "pv-zonal"),
        )
        collected = self.collected(pods, claims=claims, volumes=(local, zonal))
        stays = {pod.source.name: pod.stays for pod in collected.state.pods}
        self.assertEqual(
            stays, {"db-0": LOCAL_VOLUME, "db-1": VOLUME_UNKNOWN, "cache-0": None, "app-1": NAMESPACES_BY_LABEL}
        )
        cache = next(pod for pod in collected.state.pods if pod.source.name == "cache-0")
        self.assertEqual(cache.volumes, (zonal,))

    def test_pending_pods_and_pods_on_nodes_that_are_not_machines_are_left_out(self) -> None:
        """A pod not placed yet holds no machine, nor does one on a Fargate node."""
        nodes = [
            node("aks-apps-0", {"kubernetes.azure.com/agentpool": "apps"}),
            node("virtual-node", {"type": "virtual-kubelet"}),
        ]
        pods = [
            api_pod("web-1", node="aks-apps-0"),
            api_pod("web-2", node=None),
            api_pod("burst-1", node="virtual-node"),
        ]
        collected = collect(AZURE, ClusterRead(snapshot(nodes, pods), (agent_pool("apps"),), (), ()))
        self.assertEqual([pod.source.name for pod in collected.state.pods], ["web-1"])
        self.assertEqual(
            (collected.state.pods[0].os, collected.state.pods[0].arch, collected.not_machines), ("linux", "amd64", 1)
        )
