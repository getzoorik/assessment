"""Made-up AKS and EKS clusters for the Kubernetes part, and the answers each read gets for them.

A scenario holds the scopes the part reads, whether the run has --no-invoke, and the answers to its az, aws, kubectl
and kubelogin calls and its price-list fetches. Names, ids and prices are neutral and round, so each figure a golden
holds can be worked out by hand from these answers.
"""

import hashlib
import json
import urllib.parse
from dataclasses import dataclass, replace
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from tests.replay.clusters import (
    AKS,
    SAMPLES,
    Cluster,
    autoscaler,
    budget,
    container,
    direct_calls,
    invoke_call,
    invoke_logs,
    invoke_text,
    kubectl,
    node,
    owned,
    owner,
    pod,
    small_objects,
    top,
)
from tests.replay.disks import skus
from tests.replay.recording import TEMP, Body, Call, Failure, Fetch
from tests.replay.scenarios import (
    ACCOUNT,
    AZURE_SCOPE,
    EU,
    GROUP,
    REGION,
    SUB,
    US,
    aws_call,
    aws_failure,
    az_call,
    az_failure,
    call,
    cluster_list,
    instance,
    instances_args,
)
from zoorik.cloud.aws import AWS, AwsScope
from zoorik.cloud.azure import AZURE, AzureScope
from zoorik.kubectl.api import PAGE_SIZE
from zoorik.kubectl.objects import BUDGETS, Json
from zoorik.kubectl.usage import SampleWindow
from zoorik.pricing.retail import RETAIL_PRICES_URL

# One sample, at the start, so a cluster read directly answers one kubectl top.
WINDOW = SampleWindow(0)
VERSION = "1.30.4"
D4S = "Standard_D4s_v5"
F4S = "Standard_F4s_v2"
TYPE_LABEL = "node.kubernetes.io/instance-type"
# What a node of each EKS machine type reports: its capacity, and what EKS's reservation for the VPC CNI's max pods
# (29 and 58) and the eviction threshold leave allocatable.
EKS_NODES = {
    "m6i.large": (
        {"cpu": "2", "memory": "7910580Ki", "pods": "29"},
        {"cpu": "1930m", "memory": "7220404Ki", "pods": "29"},
    ),
    "m6i.xlarge": (
        {"cpu": "4", "memory": "15821160Ki", "pods": "58"},
        {"cpu": "3920m", "memory": "14804328Ki", "pods": "58"},
    ),
}
EKS_KUBELET = "v1.30.4-eks-a737599"
EKS_ZONES = ("eu-west-1a", "eu-west-1b", "eu-west-1c")
PRIVATE = {"enablePrivateCluster": True}
RANGES = {"enablePrivateCluster": False, "authorizedIpRanges": ["203.0.113.0/24"]}
REFUSED_AZURE = (
    "ERROR: (AuthorizationFailed) The client does not have authorization to perform action "
    "'Microsoft.ContainerService/managedClusters/read'.\n"
)
REFUSED_AWS = "An error occurred (AccessDeniedException) when calling the operation: User is not authorized.\n"
REFUSED_BUDGETS = (
    'Error from server (Forbidden): poddisruptionbudgets.policy is forbidden: User "someone" cannot list resource '
    '"poddisruptionbudgets" in API group "policy" at the cluster scope\n'
)
TIMED_OUT = "Unable to connect to the server: dial tcp 192.0.2.10:443: i/o timeout\n"


@dataclass(frozen=True)
class KubernetesScenario:
    """A made-up estate: the scopes the Kubernetes part reads, --no-invoke, and the answers to its calls and fetches."""

    name: str
    azure: Optional[AzureScope]
    aws: Optional[AwsScope]
    no_invoke: bool
    calls: Sequence[Call]
    fetches: Sequence[Fetch]


def kubeconfig(*identity: str) -> str:
    """Give the kubeconfig the run writes for a cluster in its temporary folder, named from the cluster's identity."""
    digest = hashlib.sha256("\n".join(identity).encode("utf-8")).hexdigest()[:16]
    return f"{TEMP}/kube/{digest}.yaml"


def not_installed(argv: Sequence[str]) -> Call:
    """Make a call whose program is not installed."""
    return Call(json.dumps(list(argv)), list(argv), Failure("ToolNotInstalled", f"{argv[0]} is not installed"), {}, 0.0)


# ---------------------------------------------------------------- AKS answers


def agent_pool(name: str, mode: str, count: int) -> Json:
    """Make an agent pool of D4s v5 machines as az aks list gives it, its autoscaler's minimum at today's count."""
    return {
        "name": name,
        "mode": mode,
        "vmSize": D4S,
        "count": count,
        "enableAutoScaling": True,
        "minCount": count,
        "maxCount": 2 * count,
        "osType": "Linux",
        "availabilityZones": ["1"],
        "maxPods": 110,
        "currentOrchestratorVersion": VERSION,
    }


def aks_record(name: str, **changes: object) -> Json:
    """Make a running cluster's record as az aks list gives it, with a System pool of two, and changes."""
    record: Json = {
        "name": name,
        "resourceGroup": GROUP,
        "location": REGION,
        "powerState": {"code": "Running"},
        "kubernetesVersion": VERSION,
        "agentPoolProfiles": [agent_pool("system", "System", 2)],
    }
    record.update(changes)
    return record


def aks_list(*records: Json) -> Call:
    """Make the subscription's cluster list."""
    return az_call(["aks", "list", "--subscription", SUB], list(records))


def credentials(name: str) -> Call:
    """Make az aks get-credentials writing a cluster's kubeconfig in the run's temporary folder."""
    path = kubeconfig("aks", SUB, GROUP, name)
    args = ["aks", "get-credentials", "--subscription", SUB, "--resource-group", GROUP, "--name", name]
    return call(["az", *args, "--file", path, "--overwrite-existing", *AZURE.output])


def machine_item(sku: str, product: str, meter: str, price: float) -> Json:
    """Make one retail price of a machine type in the region, per hour."""
    return {
        "armSkuName": sku,
        "productName": product,
        "meterName": meter,
        "unitOfMeasure": "1 Hour",
        "unitPrice": price,
        "tierMinimumUnits": 0.0,
    }


def machine_prices(*items: Json) -> Fetch:
    """Make the region's retail prices fetch of the machine types items price, named in the query in sorted order."""
    names = " or ".join(f"armSkuName eq '{sku}'" for sku in sorted({str(item["armSkuName"]) for item in items}))
    query = (
        f"armRegionName eq '{REGION}' and serviceName eq 'Virtual Machines' and priceType eq 'Consumption' "
        f"and ({names})"
    )
    url = RETAIL_PRICES_URL + "?" + urllib.parse.urlencode({"$filter": query})
    return Fetch(url, Body({"Items": list(items), "NextPageLink": None}))


# A D4s v5 machine costs $0.20 an hour, or $0.04 on spot; an F4s v2 costs $0.17.
D4S_ITEMS = (
    machine_item(D4S, "Virtual Machines Dsv5 Series", "D4s v5", 0.2),
    machine_item(D4S, "Virtual Machines Dsv5 Series", "D4s v5 Spot", 0.04),
)
MACHINE_PRICES = machine_prices(*D4S_ITEMS)
CANDIDATE_PRICES = machine_prices(*D4S_ITEMS, machine_item(F4S, "Virtual Machines FSv2 Series", "F4s v2", 0.17))


def vm_type(
    name: str,
    family: str,
    capabilities: Mapping[str, str],
    zones: Sequence[str] = ("1", "2", "3"),
    restrictions: Sequence[Json] = (),
) -> Json:
    """Make a VM size's Compute SKU item: its family and capabilities, its zones, and its restrictions."""
    return {
        "resourceType": "virtualMachines",
        "name": name,
        "tier": "Standard",
        "family": family,
        "locations": [REGION],
        "locationInfo": [{"location": REGION, "zones": list(zones), "zoneDetails": []}],
        "capabilities": [{"name": key, "value": value} for key, value in capabilities.items()],
        "restrictions": list(restrictions),
    }


def sized(vcpus: int, memory_gb: int, architecture: str = "x64") -> Dict[str, str]:
    """Give a VM size's capabilities: its vCPUs, memory and architecture, with premium disks and 8 data disks."""
    return {
        "vCPUs": str(vcpus),
        "MemoryGB": str(memory_gb),
        "CpuArchitectureType": architecture,
        "PremiumIO": "True",
        "MaxDataDiskCount": "8",
    }


NOT_FOR_SUBSCRIPTION = {
    "type": "Location",
    "values": [REGION],
    "restrictionInfo": {"locations": [REGION]},
    "reasonCode": "NotAvailableForSubscription",
}
# The region's VM sizes. Step 3 may move a pool to D4s v5 or F4s v2. The others are left out: B4ms bursts, D4ps v5 is
# Arm64, NC4as T4 v3 has a GPU, A1 v2 is below AKS's minimum, E4s v5 is not offered in zone 1, and the subscription may
# not use D4as v5.
VM_TYPES = (
    vm_type(D4S, "standardDSv5Family", sized(4, 16)),
    vm_type(F4S, "standardFSv2Family", sized(4, 8)),
    vm_type("Standard_B4ms", "standardBSFamily", sized(4, 16)),
    vm_type("Standard_D4ps_v5", "standardDPSv5Family", sized(4, 16, "Arm64")),
    vm_type("Standard_NC4as_T4_v3", "standardNCASv3_T4Family", {**sized(4, 28), "GPUs": "1"}),
    vm_type("Standard_A1_v2", "standardAv2Family", {**sized(1, 2), "PremiumIO": "False", "MaxDataDiskCount": "2"}),
    vm_type("Standard_E4s_v5", "standardESv5Family", sized(4, 32), zones=("2", "3")),
    vm_type("Standard_D4as_v5", "standardDASv5Family", sized(4, 16), restrictions=[NOT_FOR_SUBSCRIPTION]),
)


def aks_node(name: str, pool: str = "system", taints: Sequence[Json] = ()) -> Json:
    """Make a D4s v5 node of an agent pool in zone 1, with what AKS's reservation for 110 pods leaves allocatable."""
    record = node(name, "westeurope-1")
    record["metadata"]["labels"]["kubernetes.azure.com/agentpool"] = pool
    record["status"]["allocatable"]["memory"] = "13986964Ki"
    if taints:
        record["spec"]["taints"] = list(taints)
    return record


def worked_figure(window: SampleWindow = WINDOW) -> Cluster:
    """Make five machines; A's eight pods ask 1160m and 1812 MiB each, B's four 1160m and 4372 MiB; all peak at 700m.

    Today three pods fit a machine: A1-A3, A4-A6, A7 A8 B1, B2 B3, and B4. Each pod's peak memory is 1000 MiB, in each
    of the window's samples.
    """
    layout = {
        "node-1": ["a-1", "a-2", "a-3"],
        "node-2": ["a-4", "a-5", "a-6"],
        "node-3": ["a-7", "a-8", "b-1"],
        "node-4": ["b-2", "b-3"],
        "node-5": ["b-4"],
    }
    replica_sets = {"a": "a-5d8f9c7b6", "b": "b-7c6d5e4f3"}
    memory = {"a": "1812Mi", "b": "4372Mi"}
    pods = [
        pod(
            "shop",
            name,
            machine,
            [container("app", {"cpu": "1160m", "memory": memory[name[0]]})],
            labels={"app": name[0]},
            owners=[owner("ReplicaSet", replica_sets[name[0]])],
        )
        for machine, names in layout.items()
        for name in names
    ]
    objects: Dict[str, List[Json]] = {
        "nodes": [aks_node(machine) for machine in layout],
        "pods": pods,
        "replicasets": [owned("shop", name, [owner("Deployment", app)]) for app, name in replica_sets.items()],
        "jobs": [],
        "persistentvolumeclaims": [],
        "persistentvolumes": [],
        "csinodes": [],
        "limitranges": [],
        "horizontalpodautoscalers": [],
        "poddisruptionbudgets": [],
    }
    sample = top(*(f"shop  {name}  app  700m  1000Mi" for names in layout.values() for name in names))
    return Cluster(objects, (sample,) * window.samples)


def quiet_hours() -> Cluster:
    """Make a System pool of one machine that only critical add-ons tolerate, and a pool apps of six holding three apps.

    web, api and cart each run 4 copies, two to a machine, that ask 1500m and 2 GiB and peak at 900m and 1000 MiB.
    api has an autoscaler of its own, and a disruption budget keeps 2 of cart's copies available.
    """
    critical = [{"key": "CriticalAddonsOnly", "value": "true", "effect": "NoSchedule"}]
    machines = [f"apps-{number}" for number in range(1, 7)]
    replica_sets = {"web": "web-7b9c8d6f5", "api": "api-6c8d7e5f4", "cart": "cart-5d7e6f4a3"}
    copies = [(app, f"{app}-{number}") for app in replica_sets for number in range(1, 5)]
    pods = [
        pod(
            "shop",
            name,
            machines[index // 2],
            [container("app", {"cpu": "1500m", "memory": "2Gi"})],
            labels={"app": app},
            owners=[owner("ReplicaSet", replica_sets[app])],
        )
        for index, (app, name) in enumerate(copies)
    ]
    objects: Dict[str, List[Json]] = {
        "nodes": [aks_node("system-1", taints=critical), *(aks_node(machine, "apps") for machine in machines)],
        "pods": pods,
        "replicasets": [owned("shop", name, [owner("Deployment", app)]) for app, name in replica_sets.items()],
        "jobs": [],
        "persistentvolumeclaims": [],
        "persistentvolumes": [],
        "csinodes": [],
        "limitranges": [],
        "horizontalpodautoscalers": [autoscaler("shop", "api")],
        "poddisruptionbudgets": [budget("shop", "cart", {"matchLabels": {"app": "cart"}}, minAvailable=2)],
    }
    return Cluster(objects, (top(*(f"shop  {name}  app  900m  1000Mi" for _, name in copies)),))


def azure_scenario(name: str, calls: Sequence[Call], fetches: Sequence[Fetch] = ()) -> KubernetesScenario:
    """Make an Azure scenario run without --no-invoke."""
    return KubernetesScenario(name, AZURE_SCOPE, None, False, calls, fetches)


def invoked(scenario: str, name: str, access: Mapping[str, object]) -> KubernetesScenario:
    """Make a scenario whose one cluster, with access settings that need command invoke, is read through it.

    Its nodes run 1.30.4 but show the allocatable memory of AKS's reservation before 1.29, so step 3 is not counted.
    """
    logs = invoke_logs(invoke_text(Cluster(small_objects(), SAMPLES[:1])))
    calls = [
        aks_list(aks_record(name, apiServerAccessProfile=access)),
        invoke_call(WINDOW, logs, replace(AKS, name=name)),
        skus(*VM_TYPES),
    ]
    return azure_scenario(scenario, calls, (MACHINE_PRICES,))


# ---------------------------------------------------------------- AKS scenarios


def aks_public(window: SampleWindow = WINDOW, others: Sequence[Json] = ()) -> KubernetesScenario:
    """Read a public cluster with kubectl over window: the worked figure, $730 a month to $438, then $372, then $248.

    Each pod asks 910m and 1300 MiB after step 1, so four fit a machine and the twelve need three; the System pool keeps
    three of its five machines, below its autoscaler's minimum of five. A frees 2000m and 4096 MiB and B 1000m and
    12288 MiB, so A's share is $134 and B's $158. Step 3: an F4s v2 gives pods 3860m and 5857 MiB, so four fit one
    and the twelve need three F4s v2 at $0.17 an hour: $372, a saving of $66. Step 4 saves nothing, with one pool. Step
    5: at quiet times A and B run 2 copies each, and the four fit one F4s v2: 730 x (0.5 x 3 + 0.5 x 1) x $0.17 is
    $248, a saving of $124. Step 6 saves nothing, as a System pool stays regular. The subscription's other clusters are
    others.
    """
    name = "cluster-public"
    pools = [agent_pool("system", "System", 5)]
    record = aks_record(name, agentPoolProfiles=pools, apiServerAccessProfile={"enablePrivateCluster": False})
    reads = direct_calls(worked_figure(window), kubeconfig=kubeconfig("aks", SUB, GROUP, name))
    calls = [aks_list(record, *others), credentials(name), *reads, skus(*VM_TYPES)]
    return azure_scenario("aks-public", calls, (CANDIDATE_PRICES,))


def quiet_hours_reads(refused: bool) -> List[Call]:
    """Make the answers for reading the quiet-hours cluster, with its disruption budgets' list refused when refused.

    The region offers D4s v5 alone, so step 3 has no cheaper type to count.
    """
    name = "cluster-quiet"
    pools = [agent_pool("system", "System", 1), agent_pool("apps", "User", 6)]
    record = aks_record(name, agentPoolProfiles=pools, apiServerAccessProfile={"enablePrivateCluster": False})
    path = kubeconfig("aks", SUB, GROUP, name)
    reads = direct_calls(quiet_hours(), kubeconfig=path)
    if refused:
        budgets = kubectl("get", "--raw", f"{BUDGETS.path}?limit={PAGE_SIZE}", kubeconfig=path)
        refusal = call(budgets, exit_code=1, stderr=REFUSED_BUDGETS)
        reads = [refusal if each.argv == budgets else each for each in reads]
    return [aks_list(record), credentials(name), *reads, skus(VM_TYPES[0])]


def aks_quiet_hours() -> KubernetesScenario:
    """Read the quiet-hours cluster with kubectl: $1,022 a month to $730, then $657 with fewer copies, then $482.

    The System pool's machine stays, as no app tolerates its taint, and stays regular. After step 1 each pod asks 1170m
    and 1300 MiB, so three fit a machine and the twelve need four of apps' six, a saving of $292. At quiet times web
    runs 2 copies and cart 3, one more than its budget's minAvailable; api keeps its 4, which its autoscaler sets. The
    nine fit three machines: 730 x (0.5 x 4 + 0.5 x 3) x $0.20 is $511 for apps, a saving of $73. Those three run spot
    at $0.04 then: 730 x (0.5 x 4 x 0.20 + 0.5 x 3 x 0.04) is $336, a saving of $175.
    """
    return azure_scenario("aks-quiet-hours", quiet_hours_reads(False), (MACHINE_PRICES,))


def aks_quiet_hours_not_counted() -> KubernetesScenario:
    """Read the quiet-hours cluster with its disruption budgets refused and no spot price: $1,022 to $730, then no more.

    Without its budgets no app is counted at fewer copies, and without a spot price apps' four machines stay regular at
    quiet times too; the cluster names both reasons.
    """
    return azure_scenario("aks-quiet-hours-not-counted", quiet_hours_reads(True), (machine_prices(D4S_ITEMS[0]),))


def aks_private() -> KubernetesScenario:
    """Read a private cluster through command invoke."""
    return invoked("aks-private", "cluster-private", PRIVATE)


def aks_authorized_ranges() -> KubernetesScenario:
    """Read a cluster whose API server takes only authorized IP ranges through command invoke."""
    return invoked("aks-authorized-ranges", "cluster-ranges", RANGES)


def aks_no_invoke() -> KubernetesScenario:
    """Name the private cluster and the cluster limited to authorized IP ranges under --no-invoke: neither is read."""
    private = aks_record("cluster-private", apiServerAccessProfile=PRIVATE)
    ranges = aks_record("cluster-ranges", apiServerAccessProfile=RANGES)
    return KubernetesScenario("aks-no-invoke", AZURE_SCOPE, None, True, [aks_list(private, ranges)], ())


def aks_run_command_off() -> KubernetesScenario:
    """Name a private cluster whose command invoke is turned off: it is not read."""
    record = aks_record("cluster-private", apiServerAccessProfile={**PRIVATE, "disableRunCommand": True})
    return azure_scenario("aks-run-command-off", [aks_list(record)])


def aks_stopped() -> KubernetesScenario:
    """Name a stopped cluster: it is not read."""
    record = aks_record("cluster-stopped", powerState={"code": "Stopped"})
    return azure_scenario("aks-stopped", [aks_list(record)])


def aks_entra_without_kubelogin() -> KubernetesScenario:
    """Name a cluster that signs users in through Entra ID when kubelogin is not installed: no other way is tried."""
    name = "cluster-entra"
    record = aks_record(name, aadProfile={"managed": True, "enableAzureRbac": True})
    convert = ["kubelogin", "convert-kubeconfig", "-l", "azurecli", "--kubeconfig", kubeconfig("aks", SUB, GROUP, name)]
    return azure_scenario("aks-entra-without-kubelogin", [aks_list(record), credentials(name), not_installed(convert)])


def aks_list_refused() -> KubernetesScenario:
    """Name the subscription whose cluster list is refused: its clusters are not listed."""
    return azure_scenario("aks-list-refused", [az_failure(["aks", "list", "--subscription", SUB], REFUSED_AZURE)])


# ---------------------------------------------------------------- EKS answers


def eks_cluster(name: str, status: str = "ACTIVE", public: bool = True) -> Call:
    """Make an EKS cluster's record: its status, and whether its endpoint answers from the internet too."""
    vpc = {"endpointPublicAccess": public, "endpointPrivateAccess": True, "publicAccessCidrs": ["0.0.0.0/0"]}
    record = {"name": name, "status": status, "version": "1.30", "resourcesVpcConfig": vpc}
    return aws_call(["eks", "describe-cluster", "--name", name, "--region", EU], {"cluster": record})


def node_groups(cluster: str, groups: Sequence[Tuple[str, str]]) -> List[Call]:
    """Make a cluster's managed node groups of m6i.large machines: each one's name and capacity type."""
    names = [name for name, _ in groups]
    calls = [aws_call(["eks", "list-nodegroups", "--cluster-name", cluster, "--region", EU], {"nodegroups": names})]
    for name, capacity in groups:
        record = {
            "nodegroupName": name,
            "capacityType": capacity,
            "instanceTypes": ["m6i.large"],
            "amiType": "AL2023_x86_64_STANDARD",
            # Its autoscaler's minimum, which the steps don't keep.
            "scalingConfig": {"minSize": 2, "maxSize": 4, "desiredSize": 2},
        }
        args = ["eks", "describe-nodegroup", "--cluster-name", cluster, "--nodegroup-name", name, "--region", EU]
        calls.append(aws_call(args, {"nodegroup": record}))
    return calls


def update_kubeconfig(cluster: str) -> Call:
    """Make aws eks update-kubeconfig writing a cluster's kubeconfig in the run's temporary folder."""
    args = ["eks", "update-kubeconfig", "--name", cluster, "--kubeconfig", kubeconfig("eks", EU, cluster)]
    return call(["aws", *args, "--region", EU, *AWS.output])


def unreachable(cluster: str) -> Call:
    """Make the /version check of a cluster whose API server doesn't answer from here."""
    argv = kubectl("get", "--raw", "/version", request_timeout="20s", kubeconfig=kubeconfig("eks", EU, cluster))
    return call(argv, exit_code=1, stderr=TIMED_OUT)


def eks_node(name: str, zone: str, provider: str, labels: Mapping[str, str]) -> Json:
    """Make an EKS node in zone with its provider id and labels; a node with a machine type reports that type's room."""
    record = node(name, zone)
    record["metadata"]["labels"] = {
        "kubernetes.io/hostname": name,
        "kubernetes.io/os": "linux",
        "kubernetes.io/arch": "amd64",
        "topology.kubernetes.io/zone": zone,
        **labels,
    }
    record["spec"] = {"providerID": provider}
    if TYPE_LABEL in labels:
        capacity, allocatable = EKS_NODES[labels[TYPE_LABEL]]
        record["status"]["capacity"].update(capacity)
        record["status"]["allocatable"].update(allocatable)
        record["status"]["nodeInfo"]["kubeletVersion"] = EKS_KUBELET
    return record


def app_pod(namespace: str, name: str, machine: str, request: Tuple[str, str], controller: Tuple[str, str]) -> Json:
    """Make a pod of one container on machine, with its CPU and memory request and its controller."""
    requests = {"cpu": request[0], "memory": request[1]}
    return pod(namespace, name, machine, [container("app", requests)], owners=[owner(*controller)])


def node_kinds(window: SampleWindow = WINDOW) -> Cluster:
    """Make a cluster with a node of each kind, and one pod on each machine but the Fargate node, which has its own.

    web's pods ask 500m and 1 GiB and peak at 100m and 200 MiB; worker's ask as much and peak at 200m and 400 MiB, in
    each of the window's samples. report and tools have no reading, so they keep their requests.
    """
    group = "eks.amazonaws.com/nodegroup"
    on_demand = {TYPE_LABEL: "m6i.large", group: "apps-on-demand", "eks.amazonaws.com/capacityType": "ON_DEMAND"}
    spot = {TYPE_LABEL: "m6i.large", group: "apps-spot", "eks.amazonaws.com/capacityType": "SPOT"}
    burst = {
        TYPE_LABEL: "m6i.xlarge",
        "karpenter.sh/nodepool": "burst",
        "karpenter.sh/capacity-type": "on-demand",
        "karpenter.k8s.aws/instance-family": "m6i",
    }
    nodes = [
        eks_node("on-demand-1", "eu-west-1a", "aws:///eu-west-1a/i-02", on_demand),
        eks_node("on-demand-2", "eu-west-1b", "aws:///eu-west-1b/i-03", on_demand),
        eks_node("spot-1", "eu-west-1a", "aws:///eu-west-1a/i-04", spot),
        eks_node("spot-2", "eu-west-1b", "aws:///eu-west-1b/i-05", spot),
        eks_node("burst-1", "eu-west-1a", "aws:///eu-west-1a/i-06", burst),
        eks_node("self-1", "eu-west-1a", "aws:///eu-west-1a/i-01", {TYPE_LABEL: "m6i.large"}),
        eks_node(
            "fargate-1", "eu-west-1a", "aws:///eu-west-1a/fargate-1", {"eks.amazonaws.com/compute-type": "fargate"}
        ),
    ]
    small, large = ("500m", "1Gi"), ("1", "2Gi")
    pods = [
        app_pod("shop", "web-1", "on-demand-1", small, ("ReplicaSet", "web-6f7d8c9b5")),
        app_pod("shop", "web-2", "on-demand-2", small, ("ReplicaSet", "web-6f7d8c9b5")),
        app_pod("shop", "worker-1", "spot-1", small, ("ReplicaSet", "worker-5c6b7a8d9")),
        app_pod("shop", "worker-2", "spot-2", small, ("ReplicaSet", "worker-5c6b7a8d9")),
        app_pod("batch", "report-29000000-x7k2p", "burst-1", large, ("Job", "report-29000000")),
        app_pod("shop", "tools-1", "self-1", ("250m", "512Mi"), ("ReplicaSet", "tools-7d8e9f0a1")),
        app_pod("shop", "edge-1", "fargate-1", small, ("ReplicaSet", "edge-5b6c7d8e9")),
    ]
    sets = {"web-6f7d8c9b5": "web", "worker-5c6b7a8d9": "worker", "tools-7d8e9f0a1": "tools", "edge-5b6c7d8e9": "edge"}
    objects: Dict[str, List[Json]] = {
        "nodes": nodes,
        "pods": pods,
        "replicasets": [owned("shop", name, [owner("Deployment", app)]) for name, app in sets.items()],
        "jobs": [owned("batch", "report-29000000", [owner("CronJob", "report")])],
        "persistentvolumeclaims": [],
        "persistentvolumes": [],
        "csinodes": [],
        "limitranges": [],
        "horizontalpodautoscalers": [],
        "poddisruptionbudgets": [],
    }
    sample = top(
        "shop  web-1  app  100m  200Mi",
        "shop  web-2  app  100m  200Mi",
        "shop  worker-1  app  200m  400Mi",
        "shop  worker-2  app  200m  400Mi",
    )
    return Cluster(objects, (sample,) * window.samples)


def product(machine_type: str, price: str) -> str:
    """Make one Price List product of a machine type, with its on-demand price per hour, as the text it gives."""
    dimension = {"unit": "Hrs", "pricePerUnit": {"USD": price}, "beginRange": "0", "endRange": "Inf"}
    terms = {"OnDemand": {"term-one": {"priceDimensions": {"dimension-one": dimension}}}}
    return json.dumps({"product": {"attributes": {"instanceType": machine_type, "regionCode": EU}}, "terms": terms})


def spot_price(machine_type: str, zone: str, price: str) -> Json:
    """Make one entry of the region's spot price history, at the start of the run's hour."""
    return {
        "AvailabilityZone": zone,
        "InstanceType": machine_type,
        "ProductDescription": "Linux/UNIX",
        "SpotPrice": price,
        "Timestamp": "2026-01-05T09:00:00+00:00",
    }


def ec2_type(name: str, size: Tuple[int, int], network: Tuple[int, int], **fields: object) -> Json:
    """Make an x86_64 Nitro instance type as describe-instance-types gives it, with fields added or replaced.

    size is its vCPUs and MiB; network is its default card's interfaces and the IPv4 addresses each takes.
    """
    vcpus, memory = size
    interfaces, addresses = network
    record: Json = {
        "InstanceType": name,
        "VCpuInfo": {"DefaultVCpus": vcpus},
        "MemoryInfo": {"SizeInMiB": memory},
        "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
        "BurstablePerformanceSupported": False,
        "EbsInfo": {"NvmeSupport": "required", "MaximumEbsAttachments": 28, "AttachmentLimitType": "shared"},
        "NetworkInfo": {
            "MaximumNetworkInterfaces": interfaces,
            "Ipv4AddressesPerInterface": addresses,
            "DefaultNetworkCardIndex": 0,
            "NetworkCards": [{"NetworkCardIndex": 0, "MaximumNetworkInterfaces": interfaces}],
        },
    }
    record.update(fields)
    return record


# The region's instance types. Step 3 may move a pool to m6i.large or m6i.xlarge, whose VPC CNI max pods are 29 and
# 58. The others are left out: t3.large bursts, m7g.large is arm64, g4dn.xlarge has a GPU, and r6i.large is offered
# only in eu-west-1c, where no pool runs.
EC2_TYPES = (
    ec2_type("m6i.large", (2, 8192), (3, 10)),
    ec2_type("m6i.xlarge", (4, 16384), (4, 15)),
    ec2_type("t3.large", (2, 8192), (3, 12), BurstablePerformanceSupported=True),
    ec2_type("m7g.large", (2, 8192), (3, 10), ProcessorInfo={"SupportedArchitectures": ["arm64"]}),
    ec2_type("g4dn.xlarge", (4, 16384), (3, 10), GpuInfo={"Gpus": [{"Name": "T4", "Count": 1}]}),
    ec2_type("r6i.large", (2, 16384), (3, 10)),
)


def instance_types(*kinds: Json) -> Call:
    """Make the region's describe-instance-types answer."""
    return aws_call(["ec2", "describe-instance-types", "--region", EU], {"InstanceTypes": list(kinds)})


def type_offerings() -> Call:
    """Make the zones that offer each of EC2_TYPES: every zone of the region, but r6i.large only eu-west-1c."""
    offers = [
        {"InstanceType": kind["InstanceType"], "LocationType": "availability-zone", "Location": zone}
        for kind in EC2_TYPES
        for zone in EKS_ZONES
        if kind["InstanceType"] != "r6i.large" or zone == "eu-west-1c"
    ]
    args = ["ec2", "describe-instance-type-offerings", "--location-type", "availability-zone", "--region", EU]
    return aws_call(args, {"InstanceTypeOfferings": offers})


def ec2_prices() -> List[Call]:
    """Make the region's on-demand Price List and its spot price history.

    m6i.large costs $0.10 an hour and m6i.xlarge $0.20. m6i.large's latest spot price is 0.03 in one zone and 0.05 in
    the other, $0.04 on average; m6i.xlarge's is 0.07 and 0.09, $0.08.
    """
    terms = [
        ("regionCode", EU),
        ("operatingSystem", "Linux"),
        ("tenancy", "Shared"),
        ("preInstalledSw", "NA"),
        ("capacitystatus", "Used"),
        ("operation", "RunInstances"),
    ]
    filters = json.dumps([{"Type": "TERM_MATCH", "Field": field, "Value": value} for field, value in terms])
    listed = ["pricing", "get-products", "--service-code", "AmazonEC2", "--format-version", "aws_v1"]
    products = [product("m6i.large", "0.1000000000"), product("m6i.xlarge", "0.2000000000")]
    spot = ["describe-spot-price-history", "--product-descriptions", "Linux/UNIX"]
    at = ["--start-time", "2026-01-05T09:30:00Z", "--region", EU]
    history = [
        spot_price("m6i.large", "eu-west-1a", "0.030000"),
        spot_price("m6i.large", "eu-west-1b", "0.050000"),
        spot_price("m6i.xlarge", "eu-west-1a", "0.070000"),
        spot_price("m6i.xlarge", "eu-west-1b", "0.090000"),
    ]
    return [
        aws_call([*listed, "--filters", filters, "--region", US], {"PriceList": products}),
        aws_call(["ec2", *spot, *at], {"SpotPriceHistory": history}),
    ]


def aws_scenario(name: str, calls: Sequence[Call], regions: Sequence[str] = (EU,)) -> KubernetesScenario:
    """Make an AWS scenario in regions of the account."""
    return KubernetesScenario(name, None, AwsScope(ACCOUNT, tuple(regions)), False, calls, ())


# ---------------------------------------------------------------- EKS scenarios


def eks_node_kinds(window: SampleWindow = WINDOW, others: Sequence[Json] = ()) -> KubernetesScenario:
    """Read an EKS cluster with kubectl: managed on-demand and spot groups, a self-managed group, Karpenter and Fargate.

    Each kind of node is its own pool, priced as it is bought. web's and worker's pods each fit one machine of their
    group after step 1, so each group drops one: $73 and $29 a month, so $379 goes to $277. Step 3 moves burst from an
    m6i.xlarge to an m6i.large, which gives report's pod of 1 core and 2 GiB room enough, 1930m and 7051 MiB: $146 to
    $73, so $277 goes to $204. Each other pool is on the cheapest type that holds its pods; the spot pools compare
    candidates at spot prices. Step 4: the pods ask 2030m in all, and the two spot machines left, the cheapest per
    share, hold them in their room, so burst and apps-on-demand go: $204 to $58. The Fargate node is counted, and its
    pod left out. Only the self-managed node carries no provisioner's label, so the region's instances are read for it:
    it is spot, in the Auto Scaling group workers-self. The region's other instances are others.
    """
    name = "cluster-eks"
    worker = {
        **instance(1, "m6i.large", [], {"aws:autoscaling:groupName": "workers-self"}),
        "InstanceLifecycle": "spot",
    }
    calls = [
        cluster_list([name]),
        eks_cluster(name),
        *node_groups(name, [("apps-on-demand", "ON_DEMAND"), ("apps-spot", "SPOT")]),
        update_kubeconfig(name),
        *direct_calls(node_kinds(window), kubeconfig=kubeconfig("eks", EU, name)),
        aws_call(instances_args(EU), {"Reservations": [{"ReservationId": "r-01", "Instances": [worker, *others]}]}),
        instance_types(*EC2_TYPES),
        type_offerings(),
        *ec2_prices(),
    ]
    return aws_scenario("eks-node-kinds", calls)


def eks_named() -> KubernetesScenario:
    """Name each EKS cluster the run can't read, with the reason, and the region whose cluster list is refused.

    cluster-creating is not ACTIVE yet, and cluster-groups' node groups can't be read. The API servers of
    cluster-private and cluster-public don't answer from here; cluster-private's endpoint is private only.
    """
    names = ["cluster-creating", "cluster-groups", "cluster-private", "cluster-public"]
    calls = [
        cluster_list(names),
        eks_cluster("cluster-creating", status="CREATING"),
        eks_cluster("cluster-groups"),
        aws_failure(["eks", "list-nodegroups", "--cluster-name", "cluster-groups", "--region", EU], REFUSED_AWS),
        eks_cluster("cluster-private", public=False),
        *node_groups("cluster-private", []),
        update_kubeconfig("cluster-private"),
        unreachable("cluster-private"),
        eks_cluster("cluster-public"),
        *node_groups("cluster-public", []),
        update_kubeconfig("cluster-public"),
        unreachable("cluster-public"),
        aws_failure(["eks", "list-clusters", "--region", US], REFUSED_AWS),
    ]
    return aws_scenario("eks-named", calls, (EU, US))


SCENARIOS = (
    aks_public(),
    aks_quiet_hours(),
    aks_quiet_hours_not_counted(),
    aks_private(),
    aks_authorized_ranges(),
    aks_no_invoke(),
    aks_run_command_off(),
    aks_stopped(),
    aks_entra_without_kubelogin(),
    aks_list_refused(),
    eks_node_kinds(),
    eks_named(),
)
