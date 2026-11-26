"""Made-up clusters for the cluster readers: raw API objects, and the answers each read method gets for them.

The raw objects carry the fields a real API server sends, private ones included (env values, images, addresses), so
the tests show that the allowlist drops them. The direct read's answers are kubectl's pages; the command-invoke
read's answer is worked out in Python from the same objects, as the script's jq and awk work it out in the cluster.
"""

import base64
import gzip
import hashlib
import json
from dataclasses import dataclass
from typing import Collection, Dict, List, Mapping, Optional, Sequence, Union

from tests.replay.recording import Call
from tests.replay.redact import narrow_pod_items
from tests.replay.scenarios import call
from zoorik.cloud.azure import AZURE, AccessProfile, AksCluster
from zoorik.kubectl.api import PAGE_SIZE, Snapshot, snapshot
from zoorik.kubectl.invoke import script
from zoorik.kubectl.objects import BUDGETS, KINDS, PODS, SCALING_KINDS, Json, Kind, slim
from zoorik.kubectl.usage import DEFAULT_SAMPLE_MINUTES, MEBIBYTE, SampleWindow, Usage, peaks, top_table

KUBECONFIG = "<temp>/kube/one.yaml"
TOP = ("top", "pods", "-A", "--containers", "--no-headers")
# The cluster a command invoke reads: private, so the run reads it through command invoke.
AKS = AksCluster(
    subscription="sub-one",
    name="cluster-one",
    resource_group="group-one",
    location="westeurope",
    power="Running",
    access=AccessProfile(private_cluster=True, authorized_ip_ranges=None, run_command_disabled=None),
    entra_id=False,
    pools=(),
)
# The API server adds these to every pod that doesn't tolerate the two taints itself.
DEFAULT_TOLERATIONS = [
    {"key": "node.kubernetes.io/not-ready", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 300},
    {"key": "node.kubernetes.io/unreachable", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 300},
]


@dataclass(frozen=True)
class TopFailure:
    """A kubectl top sample that fails, with kubectl's error output."""

    stderr: str


Sample = Union[str, TopFailure]


@dataclass(frozen=True)
class Cluster:
    """A made-up cluster: its raw objects by kind name, and its kubectl top samples in order."""

    objects: Mapping[str, Sequence[Json]]
    samples: Sequence[Sample]


# ---------------------------------------------------------------- raw objects


def owner(kind: str, name: str, *, controller: bool = True) -> Json:
    """Make an owner reference, with the fields the API server stores."""
    return {"apiVersion": "apps/v1", "kind": kind, "name": name, "uid": f"uid-{name}", "controller": controller}


def node(name: str, zone: str, **changes: Json) -> Json:
    """Make a node as the API server gives it, with changes."""
    record: Dict[str, Json] = {
        "metadata": {
            "name": name,
            "uid": f"uid-{name}",
            "resourceVersion": "4096",
            "labels": {
                "kubernetes.io/hostname": name,
                "kubernetes.io/os": "linux",
                "kubernetes.io/arch": "amd64",
                "node.kubernetes.io/instance-type": "Standard_D4s_v5",
                "topology.kubernetes.io/zone": zone,
                "kubernetes.azure.com/agentpool": "system",
            },
            "annotations": {"node.alpha.kubernetes.io/ttl": "0"},
        },
        "spec": {
            "providerID": f"azure:///subscriptions/sub-one/resourceGroups/group-one-nodes/providers/"
            f"Microsoft.Compute/virtualMachineScaleSets/pool-system/virtualMachines/{name}",
            "podCIDR": "10.244.0.0/24",
        },
        "status": {
            "capacity": {"cpu": "4", "memory": "16393364Ki", "pods": "110", "ephemeral-storage": "129886128Ki"},
            "allocatable": {"cpu": "3860m", "memory": "12880532Ki", "pods": "110", "ephemeral-storage": "119703055367"},
            "nodeInfo": {"kubeletVersion": "v1.30.4", "machineID": "machine-private", "bootID": "boot-private"},
            "addresses": [{"type": "InternalIP", "address": "10.224.0.4"}],
            "images": [{"names": ["registry.example.test/private/app:1"], "sizeBytes": 1}],
        },
    }
    record.update(changes)
    return record


def container(name: str, requests: Optional[Json] = None, limits: Optional[Json] = None, **extra: Json) -> Json:
    """Make a container with an image, a command and an env value, which the run never reads."""
    record: Dict[str, Json] = {
        "name": name,
        "image": f"registry.example.test/private/{name}:1",
        "command": ["/bin/start", "--token=private-flag"],
        "env": [{"name": "TOKEN", "value": "private-value"}],
        "resources": {key: value for key, value in (("requests", requests), ("limits", limits)) if value is not None},
        "terminationMessagePolicy": "File",
    }
    record.update(extra)
    return record


def pod(namespace: str, name: str, nodename: Optional[str], containers: Sequence[Json], **changes: Json) -> Json:
    """Make a pod as the API server gives it, with changes to its metadata, spec and status given by key."""
    metadata: Dict[str, Json] = {
        "name": name,
        "namespace": namespace,
        "uid": f"uid-{name}",
        "annotations": {"note": "private-note"},
        "labels": changes.pop("labels", {"app": name}),
    }
    if "owners" in changes:
        metadata["ownerReferences"] = changes.pop("owners")
    spec: Dict[str, Json] = {
        "containers": list(containers),
        "serviceAccountName": "default",
        "tolerations": changes.pop("tolerations", DEFAULT_TOLERATIONS),
        "volumes": [
            {"name": "kube-api-access", "projected": {"sources": [{"serviceAccountToken": {"path": "token"}}]}}
        ],
    }
    if nodename is not None:
        spec["nodeName"] = nodename
    spec.update(changes.pop("spec", {}))
    status = {"phase": changes.pop("phase", "Running"), "podIP": "10.244.0.12", "hostIP": "10.224.0.4"}
    return {"metadata": metadata, "spec": spec, "status": status, **changes}


def owned(namespace: str, name: str, owners: Optional[List[Json]]) -> Json:
    """Make a ReplicaSet or a Job with its pod template, which the run never reads."""
    metadata: Dict[str, Json] = {"name": name, "namespace": namespace, "uid": f"uid-{name}"}
    if owners is not None:
        metadata["ownerReferences"] = owners
    template = {"spec": {"containers": [container("app")]}}
    return {"metadata": metadata, "spec": {"replicas": 1, "template": template}, "status": {"replicas": 1}}


def claim(namespace: str, name: str, volume: Optional[str]) -> Json:
    """Make a claim, bound to volume or not bound yet."""
    spec: Dict[str, Json] = {"accessModes": ["ReadWriteOnce"], "storageClassName": "managed-csi"}
    if volume is not None:
        spec["volumeName"] = volume
    return {"metadata": {"name": name, "namespace": namespace, "uid": f"uid-{name}"}, "spec": spec}


def volume(name: str, source: Json, zone: Optional[str] = None) -> Json:
    """Make a volume with its source, such as csi or nfs, and a zone it is bound to."""
    spec: Dict[str, Json] = {"capacity": {"storage": "64Gi"}, "claimRef": {"name": "claim", "namespace": "shop"}}
    spec.update(source)
    if zone is not None:
        term = {"matchExpressions": [{"key": "topology.disk.csi.azure.com/zone", "operator": "In", "values": [zone]}]}
        spec["nodeAffinity"] = {"required": {"nodeSelectorTerms": [term]}}
    return {"metadata": {"name": name, "uid": f"uid-{name}"}, "spec": spec}


def csi_node(name: str, drivers: List[Json]) -> Json:
    """Make a CSINode."""
    return {"metadata": {"name": name, "uid": f"uid-{name}"}, "spec": {"drivers": drivers}}


def limit_range(namespace: str, limits: List[Json]) -> Json:
    """Make a LimitRange."""
    return {"metadata": {"name": "limits", "namespace": namespace}, "spec": {"limits": limits}}


def autoscaler(namespace: str, name: str, kind: str = "Deployment") -> Json:
    """Make a HorizontalPodAutoscaler of the object of kind and name, with the fields the run never reads."""
    target = {"apiVersion": "argoproj.io/v1alpha1" if kind == "Rollout" else "apps/v1", "kind": kind, "name": name}
    metric = {
        "type": "Resource",
        "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 70}},
    }
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": f"uid-hpa-{name}",
            "annotations": {"note": "private-note"},
        },
        "spec": {"scaleTargetRef": target, "minReplicas": 2, "maxReplicas": 10, "metrics": [metric]},
        "status": {"currentReplicas": 2, "desiredReplicas": 2},
    }


def budget(namespace: str, name: str, selector: Optional[Json], **spec: Json) -> Json:
    """Make a PodDisruptionBudget whose spec is selector and spec, such as minAvailable, with its status."""
    full = dict(spec) if selector is None else {"selector": selector, **spec}
    status = {"currentHealthy": 2, "desiredHealthy": 1, "disruptionsAllowed": 1, "expectedPods": 2}
    return {
        "metadata": {"name": name, "namespace": namespace, "uid": f"uid-pdb-{name}"},
        "spec": full,
        "status": status,
    }


SPREAD = {
    "maxSkew": 1,
    "topologyKey": "topology.kubernetes.io/zone",
    "whenUnsatisfiable": "DoNotSchedule",
    "labelSelector": {"matchLabels": {"app": "web"}},
    "matchLabelKeys": ["pod-template-hash"],
}
# A preference: the scheduler doesn't hold to it, so the label it names is not kept.
SPREAD_PREFERRED = {
    "maxSkew": 1,
    "topologyKey": "kubernetes.io/hostname",
    "whenUnsatisfiable": "ScheduleAnyway",
    "labelSelector": {"matchLabels": {"tier": "front"}},
}
ANTI_AFFINITY = {
    "labelSelector": {"matchExpressions": [{"key": "app", "operator": "In", "values": ["web"]}]},
    "topologyKey": "kubernetes.io/hostname",
    "matchLabelKeys": ["version"],
}
NEAR_CACHE = {
    "labelSelector": {"matchLabels": {"role": "cache"}},
    "topologyKey": "topology.kubernetes.io/zone",
    "mismatchLabelKeys": ["track"],
}
LINUX_ONLY = {"matchExpressions": [{"key": "kubernetes.io/os", "operator": "In", "values": ["linux"]}]}
WEB_LABELS = {"app": "web", "pod-template-hash": "6d4b75cb6d", "team": "one", "version": "v2", "tier": "front"}


def web_pod(name: str, nodename: Optional[str], phase: str = "Running") -> Json:
    """Make a pod of the web Deployment: a spread constraint, a required anti-affinity and a preferred affinity."""
    preferred = [
        {
            "weight": 1,
            "podAffinityTerm": {
                "labelSelector": {"matchLabels": {"team": "one"}},
                "topologyKey": "kubernetes.io/hostname",
            },
        }
    ]
    return pod(
        "shop",
        name,
        nodename,
        [
            container("app", {"cpu": "250m", "memory": "256Mi"}, {"cpu": "500m", "memory": "512Mi"}),
            # Limits alone, which the API server never stores: it copies a limit to a missing request. The
            # reader takes the stored requests, so this container has none; it never takes the limit.
            container("proxy", None, {"cpu": "100m", "memory": "64Mi"}),
        ],
        labels=dict(WEB_LABELS),
        owners=[owner("ReplicaSet", "web-6d4b75cb6d")],
        phase=phase,
        spec={
            "topologySpreadConstraints": [SPREAD, SPREAD_PREFERRED],
            "affinity": {
                "nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [LINUX_ONLY]}},
                "podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [ANTI_AFFINITY]},
                "podAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": preferred},
            },
        },
    )


def small_objects() -> Dict[str, List[Json]]:
    """Make the objects of a small cluster with one of each case the readers meet."""
    agent_ports = [
        {"containerPort": 9100, "hostPort": 9100, "protocol": "TCP"},
        {"containerPort": 8080, "protocol": "TCP"},
    ]
    return {
        "nodes": [
            node("node-one", "westeurope-1"),
            node(
                "node-two",
                "westeurope-2",
                spec={
                    "providerID": "azure:///node-two",
                    "taints": [{"key": "dedicated", "value": "db", "effect": "NoSchedule"}],
                },
            ),
        ],
        "pods": [
            web_pod("web-6d4b75cb6d-abcde", "node-one"),
            web_pod("web-6d4b75cb6d-fghij", None, phase="Pending"),
            pod(
                "kube-system",
                "agent-x2k4p",
                "node-one",
                [container("agent", {"cpu": "50m", "memory": "64Mi"}, {"memory": "128Mi"}, ports=agent_ports)],
                labels={"app": "agent", "controller-revision-hash": "7c9d8b"},
                owners=[owner("DaemonSet", "agent")],
                tolerations=[{"operator": "Exists"}],
            ),
            pod(
                "kube-system",
                "kube-proxy-node-two",
                "node-two",
                [container("kube-proxy", {"cpu": "100m"})],
                labels={"addonmanager.kubernetes.io/mode": "Reconcile", "component": "kube-proxy"},
                owners=[owner("Node", "node-two")],
            ),
            pod(
                "batch",
                "report-28100000-x7k2p",
                "node-two",
                [container("report", {"cpu": "1", "memory": "1Gi"}, {"cpu": "2", "memory": "2Gi"})],
                owners=[owner("Job", "report-28100000")],
                spec={
                    "overhead": {"cpu": "250m", "memory": "120Mi"},
                    "initContainers": [
                        container("setup", {"cpu": "200m"}),
                        container("log-shipper", {"cpu": "50m", "memory": "32Mi"}, restartPolicy="Always"),
                    ],
                },
            ),
            pod(
                "shop",
                "db-0",
                "node-two",
                [container("db", {"cpu": "500m", "memory": "2Gi"})],
                owners=[owner("StatefulSet", "db")],
                spec={
                    "resources": {"requests": {"cpu": "1", "memory": "3Gi"}, "limits": {"memory": "4Gi"}},
                    "nodeSelector": {"kubernetes.io/os": "linux"},
                    "affinity": {"podAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [NEAR_CACHE]}},
                    "tolerations": [{"key": "dedicated", "operator": "Equal", "value": "db", "effect": "NoSchedule"}],
                    "volumes": [
                        {"name": "data", "persistentVolumeClaim": {"claimName": "data-db-0"}},
                        {"name": "config", "configMap": {"name": "db-config"}},
                    ],
                },
            ),
            pod(
                "default",
                "debug",
                "node-one",
                [container("shell", {"cpu": "10m", "memory": "16Mi"})],
                labels={"app": "debug", "role": "cache", "track": "stable", "owner": "someone"},
            ),
            # Its only owner is not its controller, so nothing recreates it.
            pod(
                "default",
                "step-one",
                "node-one",
                [container("step", {"cpu": "10m", "memory": "16Mi"})],
                owners=[owner("Workflow", "flow-one", controller=False)],
            ),
            pod("batch", "report-28099000-q9w8e", "node-two", [container("report")], phase="Succeeded"),
            pod("batch", "report-28098000-z1y2x", "node-two", [container("report")], phase="Failed"),
        ],
        "replicasets": [
            owned("shop", "web-6d4b75cb6d", [owner("Deployment", "web")]),
            owned("shop", "old-5c4b3a2190", None),
            owned("shop", "manual-7f6e5d4c3b", [owner("Deployment", "manual", controller=False)]),
        ],
        "jobs": [owned("batch", "report-28100000", [owner("CronJob", "report")]), owned("batch", "once", None)],
        "persistentvolumeclaims": [claim("shop", "data-db-0", "pv-data-db-0"), claim("shop", "waiting", None)],
        "persistentvolumes": [
            volume(
                "pv-data-db-0",
                {
                    "csi": {
                        "driver": "disk.csi.azure.com",
                        "volumeHandle": "/subscriptions/sub-one/disks/data",
                        "volumeAttributes": {"skuName": "Premium_LRS", "requestedsizegib": "64"},
                    }
                },
                zone="westeurope-2",
            ),
            volume(
                "pv-cache",
                {
                    "csi": {
                        "driver": "disk.csi.azure.com",
                        "volumeAttributes": {"StorageAccountType": "StandardSSD_LRS"},
                    }
                },
            ),
            volume(
                "pv-legacy",
                {
                    "azureDisk": {
                        "diskName": "disk-legacy",
                        "diskURI": "/subscriptions/sub-one/disks/legacy",
                        "kind": "Managed",
                    }
                },
            ),
            volume("pv-share", {"nfs": {"server": "files.example.test", "path": "/exports/private"}}),
        ],
        "csinodes": [
            csi_node(
                "node-one",
                [
                    {
                        "name": "disk.csi.azure.com",
                        "nodeID": "node-one",
                        "allocatable": {"count": 8},
                        "topologyKeys": ["zone"],
                    },
                    {"name": "file.csi.azure.com", "nodeID": "node-one"},
                ],
            ),
            csi_node("node-two", [{"name": "disk.csi.azure.com", "nodeID": "node-two", "allocatable": {"count": 8}}]),
        ],
        "limitranges": [
            limit_range(
                "shop",
                [
                    {
                        "type": "Container",
                        "min": {"cpu": "50m", "memory": "64Mi"},
                        "max": {"cpu": "2", "memory": "4Gi"},
                        "maxLimitRequestRatio": {"cpu": "4"},
                        "default": {"cpu": "500m"},
                        "defaultRequest": {"cpu": "100m"},
                    },
                    {"type": "PersistentVolumeClaim", "min": {"storage": "1Gi"}},
                ],
            )
        ],
        "horizontalpodautoscalers": [autoscaler("shop", "web")],
        "poddisruptionbudgets": [
            budget("shop", "web", {"matchLabels": {"tier": "front"}}, minAvailable=1),
            budget("shop", "db", {"matchLabels": {"role": "db"}}, maxUnavailable=1),
        ],
    }


def top(*rows: str) -> str:
    """Write a kubectl top pods -A --containers --no-headers answer from rows of five columns."""
    return "".join(f"{row}\n" for row in rows)


SAMPLES = (
    top(
        "shop  web-6d4b75cb6d-abcde  app    120m  200Mi",
        "shop  web-6d4b75cb6d-abcde  proxy  8m    30Mi",
        "kube-system  agent-x2k4p  agent  4m  40Mi",
        "batch  report-28100000-x7k2p  report  900m  700Mi",
        "shop  db-0  db  300m  1500Mi",
    ),
    top(
        "shop  web-6d4b75cb6d-abcde  app    180m  190Mi",
        "shop  web-6d4b75cb6d-abcde  proxy  6m    31Mi",
        "kube-system  agent-x2k4p  agent  5m  38Mi",
        "batch  report-28100000-x7k2p  report  950m  650Mi",
        "shop  db-0  db  280m  1520Mi",
    ),
    top(
        "shop  web-6d4b75cb6d-abcde  app    150m  230Mi",
        "shop  web-6d4b75cb6d-abcde  proxy  7m    29Mi",
        "kube-system  agent-x2k4p  agent  3m  41Mi",
        "shop  db-0  db  310m  1490Mi",
    ),
)
SMALL = Cluster(small_objects(), SAMPLES)
METRICS_MISSING = "error: Metrics API not available\n"
FORBIDDEN = (
    'Error from server (Forbidden): nodes is forbidden: User "someone" cannot list resource "nodes" in API group "" '
    "at the cluster scope\n"
)


# ---------------------------------------------------------------- the direct read's answers


def kubectl(*args: str, request_timeout: str = "60s", kubeconfig: str = KUBECONFIG) -> List[str]:
    """Give a kubectl command as the direct read runs it with kubeconfig."""
    return [
        "kubectl",
        "--kubeconfig",
        kubeconfig,
        "--cache-dir",
        "<temp>/cache",
        "--request-timeout",
        request_timeout,
        *args,
    ]


def version_call(kubeconfig: str = KUBECONFIG) -> Call:
    """Make the answer to the /version check."""
    version = {"major": "1", "minor": "30", "gitVersion": "v1.30.4", "goVersion": "go1.22.5", "platform": "linux/amd64"}
    return call(kubectl("get", "--raw", "/version", request_timeout="20s", kubeconfig=kubeconfig), json.dumps(version))


def list_calls(
    kind: Kind, items: Sequence[Json], per_page: int = PAGE_SIZE, *, kubeconfig: str = KUBECONFIG
) -> List[Call]:
    """Make the answers to kind's paged list: per_page items a page, each page but the last with a continue token."""
    pages = [list(items[start : start + per_page]) for start in range(0, len(items), per_page)] or [[]]
    calls: List[Call] = []
    for number, page in enumerate(pages):
        query = f"limit={PAGE_SIZE}" if number == 0 else f"limit={PAGE_SIZE}&continue=page{number}%3D"
        metadata: Dict[str, str] = {"resourceVersion": "4096"}
        if number + 1 < len(pages):
            metadata["continue"] = f"page{number + 1}="
        document = {"kind": kind.list_kind, "apiVersion": "v1", "metadata": metadata, "items": page}
        calls.append(call(kubectl("get", "--raw", f"{kind.path}?{query}", kubeconfig=kubeconfig), json.dumps(document)))
    return calls


def top_calls(samples: Sequence[Sample], kubeconfig: str = KUBECONFIG) -> List[Call]:
    """Make the answers to the window's kubectl top samples, in order."""
    argv = kubectl(*TOP, kubeconfig=kubeconfig)
    return [
        call(argv, sample) if isinstance(sample, str) else call(argv, exit_code=1, stderr=sample.stderr)
        for sample in samples
    ]


def direct_calls(
    cluster: Cluster, per_page: Optional[Mapping[str, int]] = None, kubeconfig: str = KUBECONFIG
) -> List[Call]:
    """Make every answer the direct read gets through kubeconfig: the version check, each kind's pages, the samples."""
    sizes = per_page or {}
    lists = [
        list_calls(kind, cluster.objects[kind.name], sizes.get(kind.name, PAGE_SIZE), kubeconfig=kubeconfig)
        for kind in (*KINDS, *SCALING_KINDS)
    ]
    return [
        version_call(kubeconfig),
        *(page for pages in lists for page in pages),
        *top_calls(cluster.samples, kubeconfig),
    ]


# ---------------------------------------------------------------- the command-invoke read's answer


def kept(kind: Kind, items: Sequence[Json], budgets: Sequence[Json]) -> List[Json]:
    """Give what kind's jq filter keeps of its items, worked out in Python; pods with the cluster's kept budgets."""
    slimmed = [slim(item, kind.item, f"{kind.resource}[{index}]") for index, item in enumerate(items)]
    passing = [item for item in slimmed if kind.test is None or kind.test.passes(item)]
    return narrow_pod_items(passing, budgets) if kind is PODS else passing


def kept_budgets(cluster: Cluster, failed: Collection[str] = ()) -> List[Json]:
    """Give the cluster's budgets as the pods' labels are narrowed with them: none when their list failed."""
    return [] if BUDGETS.name in failed else kept(BUDGETS, cluster.objects[BUDGETS.name], [])


def kept_snapshot(cluster: Cluster, usage: Usage) -> Snapshot:
    """Give the snapshot both read methods make of cluster's kept items, with usage."""
    budgets = kept_budgets(cluster)
    items = {kind.name: kept(kind, cluster.objects[kind.name], budgets) for kind in KINDS}
    scaling = {kind.name: kept(kind, cluster.objects[kind.name], budgets) for kind in SCALING_KINDS}
    return snapshot(items, scaling, usage)


def peak_table(samples: Sequence[Sample]) -> str:
    """Give the usage section the script prints, worked out in Python as its awk works it out."""
    answered = [top_table(sample) for sample in samples if isinstance(sample, str)]
    if not answered:
        failures = [sample.stderr for sample in samples if isinstance(sample, TopFailure)]
        return "!" + failures[-1][:300].replace("\n", " ")
    rows = [
        f"{key.namespace} {key.pod} {key.container} {peak.cpu}m {peak.memory // MEBIBYTE}Mi"
        for key, peak in peaks(answered).items()
    ]
    return "\n".join(rows)


def invoke_text(cluster: Cluster, failures: Optional[Mapping[str, str]] = None) -> str:
    """Give the sections the script prints before gzip: each kind's kept items, or ! and its error, then the usage."""
    failed = failures or {}
    budgets = kept_budgets(cluster, failed)
    lines: List[str] = []
    for kind in (*SCALING_KINDS, *KINDS):
        items = json.dumps(kept(kind, cluster.objects[kind.name], budgets), separators=(",", ":"))
        lines += [f"@@{kind.name}", "!" + failed[kind.name] if kind.name in failed else items]
    return "\n".join([*lines, "@@usage", peak_table(cluster.samples)]) + "\n"


def invoke_logs(text: str) -> str:
    """Wrap sections as the script prints them: gzip at its default level, in base64 lines of 76, between the marks."""
    encoded = base64.encodebytes(gzip.compress(text.encode("utf-8"), compresslevel=6, mtime=0)).decode("ascii")
    return f"@@begin\n{encoded}@@end\n"


def invoke_call(window: SampleWindow, logs: str, cluster: AksCluster = AKS) -> Call:
    """Make command invoke's answer for the read of cluster over window: a run that succeeded, with its logs."""
    argv = [
        "az",
        "aks",
        "command",
        "invoke",
        "--subscription",
        cluster.subscription,
        "--resource-group",
        cluster.resource_group,
        "--name",
        cluster.name,
        "--command",
        script(window),
        *AZURE.output,
    ]
    return call(
        argv,
        json.dumps({"id": "run-one", "provisioningState": "Succeeded", "exitCode": 0, "logs": logs, "reason": None}),
    )


# ---------------------------------------------------------------- a cluster sized like a large one

LARGE_NODES = 200
DAEMONS = ("csi-azuredisk-node", "csi-azurefile-node", "cloud-node-manager", "kube-proxy", "azure-ip-masq-agent")
# 225 Deployments, then 25 StatefulSets, spread over 40 team namespaces.
APPS = 250
DEPLOYMENTS = 225
TEAMS = 40
REPLICAS = 4
REPLICASETS_KEPT = 4


def suffix(seed: str, length: int) -> str:
    """Make a random-looking name part from seed, as Kubernetes makes pod and ReplicaSet suffixes."""
    alphabet = "bcdfghjklmnpqrstvwxz2456789"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return "".join(alphabet[byte % len(alphabet)] for byte in digest[:length])


def large_node(index: int) -> Json:
    """Make one node of a large AKS cluster, with the labels AKS puts on its nodes."""
    name = f"aks-user{index // 50}-28917403-vmss{index:06x}"
    zone = f"westeurope-{index % 3 + 1}"
    labels = {
        "agentpool": f"user{index // 50}",
        "beta.kubernetes.io/arch": "amd64",
        "beta.kubernetes.io/instance-type": "Standard_D8s_v5",
        "beta.kubernetes.io/os": "linux",
        "failure-domain.beta.kubernetes.io/region": "westeurope",
        "failure-domain.beta.kubernetes.io/zone": zone,
        "kubernetes.azure.com/agentpool": f"user{index // 50}",
        "kubernetes.azure.com/cluster": "group-one-nodes",
        "kubernetes.azure.com/consolidated-additional-properties": suffix(f"props-{index // 50}", 32),
        "kubernetes.azure.com/mode": "user",
        "kubernetes.azure.com/node-image-version": "AKSUbuntu-2204gen2containerd-202611.03.0",
        "kubernetes.azure.com/nodepool-type": "VirtualMachineScaleSets",
        "kubernetes.azure.com/os-sku": "Ubuntu",
        "kubernetes.azure.com/role": "agent",
        "kubernetes.azure.com/storageprofile": "managed",
        "kubernetes.azure.com/storagetier": "Premium_LRS",
        "kubernetes.io/arch": "amd64",
        "kubernetes.io/hostname": name,
        "kubernetes.io/os": "linux",
        "kubernetes.io/role": "agent",
        "node-role.kubernetes.io/agent": "",
        "node.kubernetes.io/instance-type": "Standard_D8s_v5",
        "storageprofile": "managed",
        "storagetier": "Premium_LRS",
        "topology.disk.csi.azure.com/zone": zone,
        "topology.kubernetes.io/region": "westeurope",
        "topology.kubernetes.io/zone": zone,
    }
    return node(name, zone, metadata={"name": name, "labels": labels})


def daemon_pod(daemon: str, nodename: str) -> Json:
    """Make one pod of a DaemonSet with three containers, as the CSI node plugins have."""
    containers = [
        container(name, {"cpu": "10m", "memory": "20Mi"}, {"memory": "200Mi"})
        for name in (daemon, "liveness-probe", "node-driver-registrar")
    ]
    terms = [{"matchFields": [{"key": "metadata.name", "operator": "In", "values": [nodename]}]}]
    tolerations = [
        {"operator": "Exists"},
        {"key": "CriticalAddonsOnly", "operator": "Exists"},
        *(
            {"key": f"node.kubernetes.io/{taint}", "operator": "Exists", "effect": "NoSchedule"}
            for taint in ("disk-pressure", "memory-pressure", "pid-pressure", "unschedulable")
        ),
        *DEFAULT_TOLERATIONS,
    ]
    return pod(
        "kube-system",
        f"{daemon}-{suffix(daemon + nodename, 5)}",
        nodename,
        containers,
        labels={"app": daemon, "controller-revision-hash": suffix(daemon, 10), "pod-template-generation": "3"},
        owners=[owner("DaemonSet", daemon)],
        tolerations=tolerations,
        spec={
            "affinity": {
                "nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": terms}}
            }
        },
    )


def app_pod(namespace: str, app: str, replica: int, nodename: str, stateful: bool) -> Json:
    """Make one pod of an app with three containers: a Deployment's, or with a claim, a StatefulSet's."""
    template = suffix(app, 10)
    name = f"{app}-{replica}" if stateful else f"{app}-{template}-{suffix(f'{app}-{replica}', 5)}"
    containers = [
        container("app", {"cpu": "250m", "memory": "512Mi"}, {"cpu": "1", "memory": "1Gi"}),
        container("metrics", {"cpu": "20m", "memory": "32Mi"}, {"memory": "64Mi"}),
        container("proxy", {"cpu": "100m", "memory": "128Mi"}, {"memory": "256Mi"}),
    ]
    spec: Dict[str, Json] = {"topologySpreadConstraints": [dict(SPREAD, labelSelector={"matchLabels": {"app": app}})]}
    if stateful:
        spec["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": f"data-{app}-{replica}"}}]
    return pod(
        namespace,
        name,
        nodename,
        containers,
        labels={"app": app, "pod-template-hash": template, "team": namespace, "version": "v3"},
        owners=[owner("StatefulSet", app) if stateful else owner("ReplicaSet", f"{app}-{template}")],
        spec=spec,
    )


def top_rows(pods: Sequence[Json], sample: int) -> str:
    """Write one kubectl top sample of every container of pods, each with use that changes from sample to sample."""
    rows = (
        f"{item['metadata']['namespace']} {item['metadata']['name']} {each['name']} "
        f"{(sample * 7 + index) % 400 + 1}m {(sample * 13 + index) % 900 + 10}Mi"
        for index, item in enumerate(pods)
        for each in item["spec"]["containers"]
    )
    return top(*rows)


def large_cluster() -> Cluster:
    """Make a cluster of 200 nodes and 2,000 pods with 6,000 containers, with the default window's samples.

    Five DaemonSets put a pod on every node; 225 Deployments and 25 StatefulSets run four replicas each. Each Deployment
    keeps its ReplicaSet and three old ones, as rollouts leave them. Every pod has three containers.
    """
    nodes = [large_node(index) for index in range(LARGE_NODES)]
    names = [item["metadata"]["name"] for item in nodes]
    pods = [daemon_pod(daemon, name) for daemon in DAEMONS for name in names]
    apps = [(f"team-{index % TEAMS:02d}", f"service-{index:03d}", index >= DEPLOYMENTS) for index in range(APPS)]
    pods += [
        app_pod(namespace, app, replica, names[(index * REPLICAS + replica) % LARGE_NODES], stateful)
        for index, (namespace, app, stateful) in enumerate(apps)
        for replica in range(REPLICAS)
    ]
    deployments = [(namespace, app) for namespace, app, stateful in apps if not stateful]
    stateful_sets = [(namespace, app) for namespace, app, stateful in apps if stateful]
    tier = {"csi": {"driver": "disk.csi.azure.com", "volumeAttributes": {"skuName": "Premium_LRS"}}}
    drivers = [{"name": "disk.csi.azure.com", "allocatable": {"count": 16}}, {"name": "file.csi.azure.com"}]
    container_max = [{"type": "Container", "max": {"cpu": "4", "memory": "8Gi"}}]
    objects = {
        "nodes": nodes,
        "pods": pods,
        "replicasets": [
            owned(namespace, f"{app}-{suffix(app + str(age), 10)}", [owner("Deployment", app)])
            for namespace, app in deployments
            for age in range(REPLICASETS_KEPT)
        ],
        "jobs": [],
        "persistentvolumeclaims": [
            claim(namespace, f"data-{app}-{replica}", f"pv-{app}-{replica}")
            for namespace, app in stateful_sets
            for replica in range(REPLICAS)
        ],
        "persistentvolumes": [
            volume(f"pv-{app}-{replica}", tier, zone=f"westeurope-{replica % 3 + 1}")
            for _, app in stateful_sets
            for replica in range(REPLICAS)
        ],
        "csinodes": [csi_node(name, drivers) for name in names],
        "limitranges": [limit_range(f"team-{index:02d}", container_max) for index in range(TEAMS)],
        "horizontalpodautoscalers": [autoscaler(namespace, app) for namespace, app in deployments[::2]],
        "poddisruptionbudgets": [
            budget(namespace, app, {"matchLabels": {"app": app}}, minAvailable=2) for namespace, app, _ in apps
        ],
    }
    return Cluster(objects, [top_rows(pods, sample) for sample in range(SampleWindow(DEFAULT_SAMPLE_MINUTES).samples)])
