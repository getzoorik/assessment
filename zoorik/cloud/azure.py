"""Azure through az: the account, AKS, Resource Graph, Azure Monitor, Run Command and the Compute SKUs.

AzureCli is the only class that builds az commands. Each answer is parsed into the dataclasses below, with a field
Optional only where Azure documents that it may leave it out or send null.
"""

import json
import urllib.parse
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

from zoorik.cloud.cli import DEFAULT_TIMEOUT, CloudCli, Host, Reason, Tool, Window, kubeconfig_file, utc_text
from zoorik.cloud.fields import Record, records_of
from zoorik.cloud.started import StartedCommands
from zoorik.errors import (
    AccessDenied,
    CommandFailed,
    CommandTimedOut,
    NotSignedIn,
    Throttled,
    UnexpectedResponse,
)
from zoorik.format import count
from zoorik.system.memo import Memo

# Rows are tried in order and the first match wins, so the specific rows come before the general ones. Throttling
# comes first: a throttled call is tried again whatever else its error says.
AZURE_REASONS = (
    Reason(
        r"too ?many ?requests|throttl|rate exceeded|\b429\b", Throttled, "Azure is throttling requests; try again later"
    ),
    Reason(
        r"runcommand/action",
        AccessDenied,
        "no permission to use Run Command (Microsoft.Compute/virtualMachines/runCommand/action)",
    ),
    Reason(r"failed to connect to msi", NotSignedIn, "Cloud Shell sign-in issue; run az login, then run this again"),
    Reason(r"aadsts", NotSignedIn, "Entra ID sign-in failed; run az login, then run this again"),
    Reason(r"please run 'az login'", NotSignedIn, "not signed in to Azure; run az login"),
    Reason(r"az login", NotSignedIn, "Azure sign-in has expired; run az login"),
    Reason(r"authorizationfailed|forbidden", AccessDenied, "access denied"),
    Reason(r"execution is in progress", CommandFailed, "another Run Command is already running on this VM"),
    Reason(r"vmagentstatuscommunicationerror", CommandFailed, "the VM agent is not reporting"),
    Reason(r"vm agent", CommandFailed, "the VM agent is not ready"),
    Reason(r"deallocat", CommandFailed, "the VM is not running"),
    Reason(
        r"vmextensionprovisioning|vmextensionhandler",
        CommandFailed,
        "Azure could not start its Run Command extension on the VM",
    ),
    Reason(r"subscriptionnotfound", CommandFailed, "subscription not found"),
    Reason(r"resourcenotfound", CommandFailed, "not found"),
    Reason(r"timed out", CommandTimedOut, "no answer in time"),
)

AZURE = Tool(
    cloud="Azure",
    output=("-o", "json", "--only-show-errors"),
    # az's own usage telemetry is off for the run's calls.
    overrides={"AZURE_CORE_COLLECT_TELEMETRY": "false"},
    reasons=AZURE_REASONS,
    cmd_script=True,
)

# az aks list answers slowly in subscriptions with many clusters; Run Command and the Compute SKUs take minutes.
CLUSTERS_TIMEOUT = 180.0
RUN_COMMAND_TIMEOUT = 300.0
SKUS_TIMEOUT = 300.0
GRAPH_URL = "/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"
# Resource Graph takes at most 1,000 subscriptions in one query, and gives at most 1,000 rows a page.
GRAPH_BATCH = 1000
SKUS_API = "2021-07-01"
METRICS_API = "2023-10-01"
# Run Command's answer marks where the script's output starts and where its errors start.
STDOUT_MARK = "[stdout]"
STDERR_MARK = "[stderr]"
# Azure's burstable sizes are the B-series, whose families all start so.
BURSTABLE_FAMILY = "standardb"

SUBSCRIPTIONS_QUERY = "resourcecontainers | where type =~ 'microsoft.resources/subscriptions' | project subscriptionId"
VM_QUERY = """resources
| where type =~ 'microsoft.compute/virtualmachines'
| project id, name, resourceGroup, subscriptionId, location, zones,
    size = tostring(properties.hardwareProfile.vmSize),
    osType = tostring(properties.storageProfile.osDisk.osType),
    power = tostring(properties.extended.instanceView.powerState.code),
    dataDisks = properties.storageProfile.dataDisks
| order by id asc"""
DISK_QUERY = """resources
| where type =~ 'microsoft.compute/disks'
| project id, subscriptionId, location, sku = tostring(sku.name), tier = tostring(properties.tier),
    sizeGb = toint(properties.diskSizeGB),
    iops = tolong(properties.diskIOPSReadWrite), mbps = tolong(properties.diskMBpsReadWrite),
    ade = tobool(properties.encryptionSettingsCollection.enabled), maxShares = toint(properties.maxShares)
| order by id asc"""
MANAGED_GROUPS_QUERY = """resourcecontainers
| where type =~ 'microsoft.resources/subscriptions/resourcegroups' and isnotempty(managedBy)
| project subscriptionId, name, managedBy
| order by subscriptionId asc, name asc"""


@dataclass(frozen=True)
class Subscription:
    """An enabled subscription, and the directory (tenant) it belongs to."""

    id: str
    name: str
    tenant: str


@dataclass(frozen=True)
class OtherDirectory:
    """A directory the signed-in account reaches but the run doesn't read, with its enabled subscriptions."""

    tenant: str
    subscriptions: Tuple[Subscription, ...]

    @property
    def note(self) -> str:
        """Say why the directory's subscriptions are not read, and how to include them."""
        them = "it" if len(self.subscriptions) == 1 else "them"
        return (
            f"{count(len(self.subscriptions), 'subscription')} in another Azure directory; to include {them}, "
            f"run az login --tenant {self.tenant} and run this again"
        )


@dataclass(frozen=True)
class AzureScope:
    """What the run reads of Azure: the signed-in directory and its enabled subscriptions."""

    # The directory's name and domain, or its id where az gives neither.
    label: str
    subscriptions: Tuple[Subscription, ...]
    elsewhere: Tuple[OtherDirectory, ...]


@dataclass(frozen=True)
class AccessProfile:
    """How a cluster's API server may be reached (apiServerAccessProfile); each setting None when not set."""

    private_cluster: Optional[bool]
    authorized_ip_ranges: Optional[Tuple[str, ...]]
    run_command_disabled: Optional[bool]


@dataclass(frozen=True)
class AgentPool:
    """One agent pool of an AKS cluster."""

    name: str
    mode: str
    # scaleSetPriority, Spot or Regular; Azure leaves it out for Regular.
    priority: Optional[str]
    max_pods: int


@dataclass(frozen=True)
class AksCluster:
    """One AKS cluster's record."""

    subscription: str
    name: str
    resource_group: str
    location: str
    # powerState.code: Running or Stopped.
    power: str
    # None for a public cluster with no access settings.
    access: Optional[AccessProfile]
    # aadProfile is set, so users sign in through Entra ID.
    entra_id: bool
    pools: Tuple[AgentPool, ...]


@dataclass(frozen=True)
class DataDiskRef:
    """A data disk as a VM's record lists it."""

    lun: int
    # managedDisk.id; None for an unmanaged (VHD) disk.
    managed_disk: Optional[str]
    # diskSizeGB as the VM record has it, which only an unmanaged disk needs.
    size_gb: Optional[int]


@dataclass(frozen=True)
class VmRecord:
    """One VM, as Resource Graph gives it."""

    id: str
    name: str
    resource_group: str
    subscription: str
    location: str
    # None for a VM in no availability zone.
    zones: Optional[Tuple[str, ...]]
    size: str
    os_type: str
    # Such as PowerState/running; empty when Azure doesn't report it.
    power: str
    data_disks: Tuple[DataDiskRef, ...]


@dataclass(frozen=True)
class DiskRecord:
    """One managed disk, as Resource Graph gives it; the performance figures are None where the disk has none."""

    id: str
    subscription: str
    location: str
    sku: str
    tier: str
    # Azure documents diskSizeGB as optional on a disk being created from another source.
    size_gb: Optional[int]
    iops: Optional[int]
    mbps: Optional[int]
    # encryptionSettingsCollection.enabled; None when the disk has no Azure Disk Encryption settings.
    ade: Optional[bool]
    # None for a disk that is not shared.
    max_shares: Optional[int]


@dataclass(frozen=True)
class ManagedGroup:
    """A resource group that another Azure resource manages, such as an AKS cluster's node group."""

    subscription: str
    name: str
    managed_by: str


@dataclass(frozen=True)
class MetricPoint:
    """One hour of one Azure Monitor metric: its busiest minute (Maximum), None for an hour with no data."""

    metric: str
    stamp: str
    maximum: Optional[float]


@dataclass(frozen=True)
class ComputeSku:
    """One VM size or disk type in a region, as offered to one subscription."""

    resource_type: str
    name: str
    family: Optional[str]
    capabilities: Mapping[str, str]
    # The zones it is offered in to the subscription; empty in a region without zones.
    zones: FrozenSet[str]
    # False when the subscription may not use it anywhere in the region.
    offered: bool
    # The zones that offer it with Ultra Disk (UltraSSDAvailable in its zone details).
    ultra_ssd_zones: FrozenSet[str]

    @property
    def vcpus(self) -> Optional[int]:
        """Give the size's vCPUs."""
        return self._integer("vCPUs")

    @property
    def memory_gb(self) -> Optional[float]:
        """Give the size's memory, in GB as Azure lists it."""
        text = self.capabilities.get("MemoryGB")
        if text is None:
            return None
        try:
            return float(text)
        except ValueError:
            raise self._unexpected("MemoryGB") from None

    @property
    def architecture(self) -> Optional[str]:
        """Give the size's CPU architecture, x64 or Arm64."""
        return self.capabilities.get("CpuArchitectureType")

    @property
    def premium_io(self) -> Optional[bool]:
        """Tell whether the size takes Premium SSD disks."""
        return self._flag("PremiumIO")

    @property
    def max_data_disks(self) -> Optional[int]:
        """Give how many data disks the size takes."""
        return self._integer("MaxDataDiskCount")

    @property
    def uncached_iops(self) -> Optional[int]:
        """Give the size's uncached disk IOPS limit."""
        return self._integer("UncachedDiskIOPS")

    @property
    def uncached_mbps(self) -> Optional[int]:
        """Give the size's uncached disk throughput limit, in MB/s of 10^6 bytes."""
        per_second = self._integer("UncachedDiskBytesPerSecond")
        return None if per_second is None else per_second // 1_000_000

    @property
    def gpus(self) -> int:
        """Give the size's GPUs; sizes without GPUs list no GPUs capability."""
        listed = self._integer("GPUs")
        return 0 if listed is None else listed

    @property
    def burstable(self) -> bool:
        """Tell whether the size is burstable (the B-series)."""
        return self.family is not None and self.family.lower().startswith(BURSTABLE_FAMILY)

    @property
    def ultra_ssd(self) -> bool:
        """Tell whether the size takes Ultra Disk in the region outside its zones (UltraSSDAvailable of the size)."""
        return self._flag("UltraSSDAvailable") is True

    def _integer(self, name: str) -> Optional[int]:
        """Read a whole-number capability; None when the SKU doesn't list it."""
        text = self.capabilities.get(name)
        if text is None:
            return None
        if not text.isdigit():
            raise self._unexpected(name)
        return int(text)

    def _flag(self, name: str) -> Optional[bool]:
        """Read a True or False capability; None when the SKU doesn't list it."""
        text = self.capabilities.get(name)
        if text is None:
            return None
        if text not in ("True", "False"):
            raise self._unexpected(name)
        return text == "True"

    def _unexpected(self, name: str) -> UnexpectedResponse:
        """Make the failure for a capability of the wrong form."""
        return UnexpectedResponse(f"unexpected answer: the {name} capability of {self.name} is not of its form")


@dataclass(frozen=True)
class RegionSkus:
    """The VM sizes and disk types of one region, as offered to one subscription."""

    # By name in lowercase: AKS and the VM records may write a size's name in another case.
    vm_sizes: Mapping[str, ComputeSku]
    disk_types: Mapping[str, ComputeSku]


class AzureCli:
    """Azure, read through az."""

    cloud = "Azure"

    def __init__(self, host: Host, started: StartedCommands) -> None:
        """Run az on host; keep each command invoke and Run Command start in started."""
        self._cli = CloudCli(AZURE, host)
        self._temp_root = host.temp_root
        self._started = started
        self._skus: Memo[Tuple[str, str], RegionSkus] = Memo(self._read_skus)

    def scope(self) -> AzureScope:
        """Read the signed-in directory and its enabled subscriptions; those of other directories are named."""
        tenant = Record(self._cli.read(["az", "account", "show"]), "az account show").text("tenantId")
        accounts = records_of(self._cli.read(["az", "account", "list"]), "az account list")
        enabled = [account for account in accounts if account.text("state") == "Enabled"]
        subscriptions = sorted((_subscription(account) for account in enabled), key=lambda sub: (sub.name, sub.id))
        others: Dict[str, List[Subscription]] = {}
        for sub in subscriptions:
            if sub.tenant != tenant:
                others.setdefault(sub.tenant, []).append(sub)
        return AzureScope(
            label=_directory_label(tenant, enabled),
            subscriptions=tuple(sub for sub in subscriptions if sub.tenant == tenant),
            elsewhere=tuple(OtherDirectory(other, tuple(subs)) for other, subs in sorted(others.items())),
        )

    def clusters(self, subscription: str) -> List[AksCluster]:
        """Read the AKS clusters of one subscription."""
        answer = self._cli.read(["az", "aks", "list", "--subscription", subscription], CLUSTERS_TIMEOUT)
        return [_cluster(subscription, record) for record in records_of(answer, "az aks list")]

    def write_kubeconfig(self, cluster: AksCluster) -> str:
        """Write the cluster's credentials to a kubeconfig in the run's temporary folder, and give its path."""
        path = kubeconfig_file(self._temp_root, ("aks", cluster.subscription, cluster.resource_group, cluster.name))
        self._cli.run(
            [
                "az",
                "aks",
                "get-credentials",
                "--subscription",
                cluster.subscription,
                "--resource-group",
                cluster.resource_group,
                "--name",
                cluster.name,
                "--file",
                path,
                "--overwrite-existing",
            ]
        )
        return path

    def invoke(self, cluster: AksCluster, script: str, timeout: float) -> str:
        """Run script inside the cluster through az aks command invoke, and give what it printed."""
        self._started.invoke(f"{cluster.subscription}/{cluster.resource_group}/{cluster.name}")
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
            script,
        ]
        answer = Record(self._cli.read(argv, timeout), "az aks command invoke")
        state = answer.text("provisioningState")
        if state != "Succeeded":
            reason = answer.optional_text("reason")
            if reason is None:
                raise CommandFailed(f"command invoke ended as {state}")
            raise self._cli.failure(reason)
        return answer.text("logs")

    def readable_subscriptions(self, subscriptions: Sequence[str]) -> FrozenSet[str]:
        """Name the subscriptions Resource Graph can read, of those given."""
        rows = self._graph(SUBSCRIPTIONS_QUERY, subscriptions)
        return frozenset(row.text("subscriptionId") for row in rows)

    def virtual_machines(self, subscriptions: Sequence[str]) -> List[VmRecord]:
        """Read every VM of the subscriptions, through Resource Graph."""
        return [_vm(row) for row in self._graph(VM_QUERY, subscriptions)]

    def managed_disks(self, subscriptions: Sequence[str]) -> List[DiskRecord]:
        """Read every managed disk of the subscriptions, through Resource Graph."""
        return [_disk(row) for row in self._graph(DISK_QUERY, subscriptions)]

    def managed_groups(self, subscriptions: Sequence[str]) -> List[ManagedGroup]:
        """Read the resource groups of the subscriptions that another Azure resource manages."""
        rows = self._graph(MANAGED_GROUPS_QUERY, subscriptions)
        return [ManagedGroup(row.text("subscriptionId"), row.text("name"), row.text("managedBy")) for row in rows]

    def run_shell_script(self, vm: str, script: str) -> str:
        """Run script in the VM, named by its resource id, through Run Command, and give what it printed."""
        self._started.run_command(vm)
        argv = ["az", "vm", "run-command", "invoke", "--ids", vm, "--command-id", "RunShellScript", "--scripts", script]
        answer = Record(self._cli.read(argv, RUN_COMMAND_TIMEOUT), "az vm run-command invoke")
        message = "".join(status.text("message") for status in answer.records("value"))
        if STDOUT_MARK not in message:
            raise UnexpectedResponse("unexpected answer: az vm run-command invoke gave no [stdout] part")
        return message.split(STDOUT_MARK, 1)[1].split(STDERR_MARK, 1)[0]

    def monitor_maxima(self, resource: str, metrics: Sequence[str], window: Window) -> List[MetricPoint]:
        """Read the busiest minute (Maximum) of each hour of the window, for each Azure Monitor metric of resource."""
        query = urllib.parse.urlencode(
            {
                "api-version": METRICS_API,
                "metricnames": ",".join(metrics),
                "timespan": f"{utc_text(window.start)}/{utc_text(window.end)}",
                "interval": "PT1H",
                "aggregation": "Maximum",
            },
            quote_via=urllib.parse.quote,
        )
        answer = Record(self._rest("get", f"{resource}/providers/Microsoft.Insights/metrics?{query}"), "Azure Monitor")
        return [
            MetricPoint(metric.record("name").text("value"), point.text("timeStamp"), point.optional_number("maximum"))
            for metric in answer.records("value")
            for series in metric.records("timeseries")
            for point in series.records("data")
        ]

    def compute_skus(self, subscription: str, region: str) -> RegionSkus:
        """Read the VM sizes and disk types the subscription may use in region; read once per run."""
        return self._skus.get((subscription, region))

    def _read_skus(self, key: Tuple[str, str]) -> RegionSkus:
        """Read the Compute SKUs of one subscription and region, following every page."""
        subscription, region = key
        url: Optional[str] = (
            f"/subscriptions/{subscription}/providers/Microsoft.Compute/skus?api-version={SKUS_API}&%24filter="
            + urllib.parse.quote(f"location eq '{region}'")
        )
        skus: List[ComputeSku] = []
        while url is not None:
            page = Record(self._rest("get", url, timeout=SKUS_TIMEOUT), "Compute SKUs")
            # Only VM sizes and disk types are read; the region lists other kinds of SKU too.
            skus.extend(_sku(item) for item in page.records("value") if item.text("resourceType") in _SKU_KINDS)
            url = page.optional_text("nextLink")
        return RegionSkus(
            vm_sizes={sku.name.lower(): sku for sku in skus if sku.resource_type == "virtualMachines"},
            disk_types={sku.name: sku for sku in skus if sku.resource_type == "disks"},
        )

    def _graph(self, query: str, subscriptions: Sequence[str]) -> List[Record]:
        """Run one Resource Graph query over the subscriptions, GRAPH_BATCH at a time, following every page."""
        rows: List[Record] = []
        for start in range(0, len(subscriptions), GRAPH_BATCH):
            batch = list(subscriptions[start : start + GRAPH_BATCH])
            options: Dict[str, object] = {"$top": GRAPH_BATCH, "resultFormat": "objectArray"}
            while True:
                body = {"subscriptions": batch, "query": query, "options": options}
                page = Record(self._rest("post", GRAPH_URL, body), "Resource Graph")
                rows.extend(page.records("data"))
                token = page.optional_text("$skipToken")
                if token is None:
                    break
                options = {**options, "$skipToken": token}
        return rows

    def _rest(self, method: str, url: str, body: Optional[object] = None, timeout: float = DEFAULT_TIMEOUT) -> object:
        """Make one Resource Manager call through az rest; a URL that starts with / goes to the signed-in cloud."""
        argv = ["az", "rest", "--method", method, "--url", url]
        if body is not None:
            argv += ["--body", json.dumps(body)]
        return self._cli.read(argv, timeout)


_SKU_KINDS = ("virtualMachines", "disks")


def _directory_label(tenant: str, accounts: Sequence[Record]) -> str:
    """Name the signed-in directory by its name and domain, as az account list gives them, else by its id."""
    for account in accounts:
        name = account.optional_text("tenantDisplayName")
        if account.text("tenantId") == tenant and name:
            domain = account.optional_text("tenantDefaultDomain")
            return name if not domain else f"{name} ({domain})"
    return tenant


def _subscription(account: Record) -> Subscription:
    """Read one subscription of az account list."""
    return Subscription(account.text("id"), account.text("name"), account.text("tenantId"))


def _optional_tuple(values: Optional[List[str]]) -> Optional[Tuple[str, ...]]:
    """Give an optional list of texts as a tuple, keeping None."""
    return None if values is None else tuple(values)


def _cluster(subscription: str, record: Record) -> AksCluster:
    """Read one record of az aks list."""
    access = record.optional_record("apiServerAccessProfile")
    return AksCluster(
        subscription=subscription,
        name=record.text("name"),
        resource_group=record.text("resourceGroup"),
        location=record.text("location"),
        power=record.record("powerState").text("code"),
        access=None if access is None else _access(access),
        entra_id=record.optional_record("aadProfile") is not None,
        pools=tuple(_pool(pool) for pool in record.records("agentPoolProfiles")),
    )


def _access(record: Record) -> AccessProfile:
    """Read a cluster's apiServerAccessProfile."""
    return AccessProfile(
        private_cluster=record.optional_flag("enablePrivateCluster"),
        authorized_ip_ranges=_optional_tuple(record.optional_texts("authorizedIpRanges")),
        run_command_disabled=record.optional_flag("disableRunCommand"),
    )


def _pool(record: Record) -> AgentPool:
    """Read one of a cluster's agentPoolProfiles."""
    return AgentPool(
        name=record.text("name"),
        mode=record.text("mode"),
        priority=record.optional_text("scaleSetPriority"),
        max_pods=record.integer("maxPods"),
    )


def _vm(row: Record) -> VmRecord:
    """Read one VM row of Resource Graph."""
    return VmRecord(
        id=row.text("id"),
        name=row.text("name"),
        resource_group=row.text("resourceGroup"),
        subscription=row.text("subscriptionId"),
        location=row.text("location"),
        zones=_optional_tuple(row.optional_texts("zones")),
        size=row.text("size"),
        os_type=row.text("osType"),
        power=row.text("power"),
        data_disks=tuple(_data_disk(disk) for disk in row.records("dataDisks")),
    )


def _data_disk(record: Record) -> DataDiskRef:
    """Read one of a VM's data disks."""
    managed = record.optional_record("managedDisk")
    return DataDiskRef(
        lun=record.integer("lun"),
        managed_disk=None if managed is None else managed.text("id"),
        size_gb=record.optional_integer("diskSizeGB"),
    )


def _disk(row: Record) -> DiskRecord:
    """Read one managed-disk row of Resource Graph."""
    return DiskRecord(
        id=row.text("id"),
        subscription=row.text("subscriptionId"),
        location=row.text("location"),
        sku=row.text("sku"),
        tier=row.text("tier"),
        size_gb=row.optional_integer("sizeGb"),
        iops=row.optional_integer("iops"),
        mbps=row.optional_integer("mbps"),
        ade=row.optional_flag("ade"),
        max_shares=row.optional_integer("maxShares"),
    )


def _capabilities(records: Sequence[Record]) -> Dict[str, str]:
    """Read a list of capabilities, each a name and a value."""
    return {capability.text("name"): capability.text("value") for capability in records}


def _ultra_ssd_zones(info: Record) -> Set[str]:
    """Give the zones of one location that offer Ultra Disk with a SKU; zoneDetails lists only zones with extras."""
    zones: Set[str] = set()
    details = info.optional_records("zoneDetails")
    for detail in () if details is None else details:
        if _capabilities(detail.records("capabilities")).get("UltraSSDAvailable") == "True":
            zones.update(detail.texts("Name"))
    return zones


def _sku(item: Record) -> ComputeSku:
    """Read one Compute SKU: its capabilities, and the zones and region it is offered in to the subscription."""
    zones: Set[str] = set()
    ultra_zones: Set[str] = set()
    for info in item.records("locationInfo"):
        listed = info.optional_texts("zones")
        if listed is not None:
            zones.update(listed)
        ultra_zones |= _ultra_ssd_zones(info)
    offered = True
    for restriction in item.records("restrictions"):
        kind = restriction.text("type")
        if kind == "Location":
            zones, offered = set(), False
        elif kind == "Zone":
            zones -= set(restriction.record("restrictionInfo").texts("zones"))
    capabilities = _capabilities(item.records("capabilities"))
    return ComputeSku(
        resource_type=item.text("resourceType"),
        name=item.text("name"),
        family=item.optional_text("family"),
        capabilities=capabilities,
        zones=frozenset(zones),
        offered=offered,
        ultra_ssd_zones=frozenset(ultra_zones),
    )
