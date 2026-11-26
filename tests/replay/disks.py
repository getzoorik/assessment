"""Made-up estates for the disk part: VMs and instances with data disks, and the answers each read gets for them.

A scenario holds the scopes the disk part reads, and the answers to its az and aws calls, its price-list fetches and
its y/N questions. Names, ids and prices are neutral and round, so each figure a golden holds can be worked out by hand
from these answers.
"""

import json
import urllib.parse
from dataclasses import dataclass
from typing import List, Mapping, Optional, Sequence, Tuple

from tests.replay.recording import Body, Call, Fetch
from tests.replay.scenarios import (
    ACCOUNT,
    AWS_SCOPE,
    AZURE_SCOPE,
    EU,
    GROUP,
    REGION,
    SUB,
    US,
    Json,
    aws_call,
    aws_failure,
    az_call,
    az_failure,
    cluster_list,
    instance,
    instances_args,
)
from zoorik.cloud import azure as azure_cloud
from zoorik.cloud.aws import AwsScope
from zoorik.cloud.azure import AzureScope
from zoorik.cloud.keys import AWS, AZURE
from zoorik.disks.guest import GUEST_SCRIPT
from zoorik.disks.metrics import AZURE_METRICS, EBS_METRICS
from zoorik.pricing.retail import RETAIL_PRICES_URL

GIB = 2**30
# The metric window of a run started at FACTS.started: the 14 days to the start of its hour.
START = "2025-12-22T09:00:00Z"
END = "2026-01-05T09:00:00Z"
# Two hours of the window, as Azure Monitor and CloudWatch write them.
HOUR_ONE = "2026-01-04T10:00:00Z"
HOUR_TWO = "2026-01-04T11:00:00Z"
CLOUDWATCH_HOUR = "2026-01-04T10:00:00+00:00"
AZURE_KERNEL = "5.15.0-1057-azure"
AWS_KERNEL = "6.1.115-126.197.amzn2023.x86_64"

# Each Premium SSD and Standard SSD tier the scenarios use: its IOPS and MB/s, as Azure sets them on the disk record.
TIER_PERFORMANCE = {
    "P10": (500, 100),
    "P15": (1100, 125),
    "P20": (2300, 150),
    "P30": (5000, 200),
    "E30": (500, 60),
}


@dataclass(frozen=True)
class DiskScenario:
    """A made-up estate: the scopes the disk part reads, and the answers to its calls, fetches and questions."""

    name: str
    azure: Optional[AzureScope]
    aws: Optional[AwsScope]
    calls: Sequence[Call]
    fetches: Sequence[Fetch]
    answers: Sequence[Optional[bool]]


# ---------------------------------------------------------------- Azure answers


def graph_args(query: str) -> List[str]:
    """Give the az rest arguments of one Resource Graph query over the subscription."""
    body = {"subscriptions": [SUB], "query": query, "options": {"$top": 1000, "resultFormat": "objectArray"}}
    return ["rest", "--method", "post", "--url", azure_cloud.GRAPH_URL, "--body", json.dumps(body)]


def azure_inventory(vms: Sequence[Json], disks: Sequence[Json], groups: Sequence[Json] = ()) -> List[Call]:
    """Make Resource Graph's answers: the subscription is readable and holds the VMs, the disks and managed groups."""
    return [
        az_call(graph_args(azure_cloud.SUBSCRIPTIONS_QUERY), {"data": [{"subscriptionId": SUB}]}),
        az_call(graph_args(azure_cloud.DISK_QUERY), {"data": list(disks)}),
        az_call(graph_args(azure_cloud.VM_QUERY), {"data": list(vms)}),
        az_call(graph_args(azure_cloud.MANAGED_GROUPS_QUERY), {"data": list(groups)}),
    ]


def vm_id(name: str, group: str = GROUP) -> str:
    """Give a VM's resource id."""
    return f"/subscriptions/{SUB}/resourceGroups/{group}/providers/Microsoft.Compute/virtualMachines/{name}"


def disk_id(name: str, group: str = GROUP) -> str:
    """Give a managed disk's resource id."""
    return f"/subscriptions/{SUB}/resourceGroups/{group}/providers/Microsoft.Compute/disks/{name}"


def vm_row(name: str, data: Sequence[Json], **changes: object) -> Json:
    """Make a Linux VM's Resource Graph row: running, size Standard_D8s_v5, in zone 1, with changes."""
    row: Json = {
        "id": vm_id(name, str(changes.get("resourceGroup", GROUP))),
        "name": name,
        "resourceGroup": GROUP,
        "subscriptionId": SUB,
        "location": REGION,
        "zones": ["1"],
        "size": "Standard_D8s_v5",
        "osType": "Linux",
        "power": "PowerState/running",
        "dataDisks": list(data),
    }
    row.update(changes)
    return row


def attached(lun: int, disk: str, size_gb: int, sku: str = "Premium_LRS") -> Json:
    """Make a managed data disk as a VM's record lists it, its id's resource group in capitals as Azure often writes."""
    return {
        "lun": lun,
        "name": disk,
        "createOption": "Attach",
        "caching": "None",
        "diskSizeGB": size_gb,
        "managedDisk": {"id": disk_id(disk, GROUP.upper()), "storageAccountType": sku},
    }


def disk_row(name: str, size_gb: int, tier: str, sku: str = "Premium_LRS", shares: Optional[int] = None) -> Json:
    """Make a managed disk's Resource Graph row, with its tier's IOPS and MB/s."""
    iops, mbps = TIER_PERFORMANCE[tier]
    return {
        "id": disk_id(name),
        "subscriptionId": SUB,
        "location": REGION,
        "sku": sku,
        "tier": tier,
        "sizeGb": size_gb,
        "iops": iops,
        "mbps": mbps,
        "ade": None,
        "maxShares": shares,
    }


def vm_size(name: str, slots: int, iops: int, bytes_per_second: int) -> Json:
    """Make a VM size's Compute SKU item, offered in the region's three zones."""
    return {
        "resourceType": "virtualMachines",
        "name": name,
        "tier": "Standard",
        "family": "standardDSv5Family",
        "locations": [REGION],
        "locationInfo": [{"location": REGION, "zones": ["1", "2", "3"], "zoneDetails": []}],
        "capabilities": [
            {"name": "MaxDataDiskCount", "value": str(slots)},
            {"name": "UncachedDiskIOPS", "value": str(iops)},
            {"name": "UncachedDiskBytesPerSecond", "value": str(bytes_per_second)},
            {"name": "PremiumIO", "value": "True"},
        ],
        "restrictions": [],
    }


# Premium SSD v2 in the region's three zones.
PREMIUM_V2 = {
    "resourceType": "disks",
    "name": "PremiumV2_LRS",
    "tier": "Premium",
    "locations": [REGION],
    "locationInfo": [{"location": REGION, "zones": ["1", "2", "3"], "zoneDetails": []}],
    "capabilities": [],
    "restrictions": [],
}
D8S = vm_size("Standard_D8s_v5", 16, 12800, 290000000)
D2S = vm_size("Standard_D2s_v5", 4, 3750, 85000000)


SKUS_ARGS = [
    "rest",
    "--method",
    "get",
    "--url",
    f"/subscriptions/{SUB}/providers/Microsoft.Compute/skus?api-version=2021-07-01&%24filter="
    + urllib.parse.quote(f"location eq '{REGION}'"),
]


def skus(*sizes: Json) -> Call:
    """Make the region's Compute SKUs answer: the sizes given, Standard_D8s_v5 when none is, and Premium SSD v2."""
    return az_call(SKUS_ARGS, {"value": [*(sizes or (D8S,)), PREMIUM_V2]})


def run_command_args(vm: str) -> List[str]:
    """Give the az arguments that run the disk part's command in a VM through Run Command."""
    return ["vm", "run-command", "invoke", "--ids", vm_id(vm), "--command-id", "RunShellScript", "--scripts"] + [
        GUEST_SCRIPT[AZURE]
    ]


def run_command(vm: str, printed: str) -> Call:
    """Make Run Command's answer, which carries what the command printed between its [stdout] and [stderr] marks."""
    status = {
        "code": "ProvisioningState/succeeded",
        "displayStatus": "Provisioning succeeded",
        "level": "Info",
        "message": f"Enable succeeded: \n[stdout]\n{printed}\n[stderr]\n",
        "time": None,
    }
    return az_call(run_command_args(vm), {"value": [status]})


def guest_output(tag: str, disks: Sequence[Tuple[str, Sequence[str]]], used_gib: Mapping[str, int], kernel: str) -> str:
    """Make what the command prints: each data disk's mount points, then the used bytes of each mount and of /."""
    lines = ["S", f"K {kernel}", "B module"]
    # lsblk prints an empty line for the disk itself, then its partitions' mount points.
    lines += [f"{tag} {disk}  " + "".join(f"{point} " for point in points) for disk, points in disks]
    lines += [f"D {gib * GIB} {mount}" for mount, gib in {"/": 8, **used_gib}.items()]
    lines.append("E")
    return "\n".join(lines) + "\n"


def hour(stamp: str, ops: Tuple[float, float], bytes_per_second: Tuple[float, float], field: str = "maximum") -> Json:
    """Make one hour of a disk's four metrics: reads and writes, operations then bytes, each its figure under field."""
    figures = (*ops, *bytes_per_second)
    return {metric: {"timeStamp": stamp, field: figure} for metric, figure in zip(AZURE_METRICS, figures)}


def monitor(disk: str, hours: Sequence[Json]) -> Call:
    """Make Azure Monitor's answer for one disk: each metric's data points, hour by hour."""
    query = urllib.parse.urlencode(
        {
            "api-version": "2023-10-01",
            "metricnames": ",".join(AZURE_METRICS),
            "timespan": f"{START}/{END}",
            "interval": "PT1H",
            "aggregation": "Maximum",
        },
        quote_via=urllib.parse.quote,
    )
    value = [
        {
            "name": {"value": metric, "localizedValue": metric},
            "unit": "CountPerSecond" if "Operations" in metric else "BytesPerSecond",
            "timeseries": [{"metadatavalues": [], "data": [point[metric] for point in hours]}],
        }
        for metric in AZURE_METRICS
    ]
    url = f"{disk_id(disk)}/providers/Microsoft.Insights/metrics?{query}"
    return az_call(["rest", "--method", "get", "--url", url], {"timespan": f"{START}/{END}", "value": value})


def retail_item(product: str, meter: str, unit: str, price: float, tier_minimum: float = 0.0) -> Json:
    """Make one item of the Azure Retail Prices API."""
    return {
        "armSkuName": "",
        "productName": product,
        "meterName": meter,
        "unitOfMeasure": unit,
        "unitPrice": price,
        "tierMinimumUnits": tier_minimum,
    }


AZURE_PRICE_QUERY = (
    "armRegionName eq 'westeurope' and priceType eq 'Consumption' and (productName eq 'Premium SSD Managed Disks' "
    "or productName eq 'Standard SSD Managed Disks' or productName eq 'Standard HDD Managed Disks' "
    "or productName eq 'Azure Premium SSD v2' or productName eq 'Ultra Disks')"
)
# The region's disk prices: Premium SSD tiers a month, and Premium SSD v2 an hour, with 3,000 IOPS and 125 MB/s free.
# Standard SSD has no price, so its disks are not priced.
AZURE_PRICES = Fetch(
    RETAIL_PRICES_URL + "?" + urllib.parse.urlencode({"$filter": AZURE_PRICE_QUERY}),
    Body(
        {
            "Items": [
                retail_item("Premium SSD Managed Disks", "P10 LRS Disk", "1/Month", 20.0),
                retail_item("Premium SSD Managed Disks", "P15 LRS Disk", "1/Month", 40.0),
                retail_item("Premium SSD Managed Disks", "P20 LRS Disk", "1/Month", 70.0),
                retail_item("Premium SSD Managed Disks", "P30 LRS Disk", "1/Month", 130.0),
                retail_item("Azure Premium SSD v2", "Premium LRS Provisioned Capacity", "1 GiB/Hour", 0.0001),
                retail_item("Azure Premium SSD v2", "Premium LRS Provisioned IOPS", "1/Hour", 0.0),
                retail_item("Azure Premium SSD v2", "Premium LRS Provisioned IOPS", "1/Hour", 0.00001, 3000.0),
                retail_item("Azure Premium SSD v2", "Premium LRS Provisioned Throughput (MBps)", "1/Hour", 0.0),
                retail_item(
                    "Azure Premium SSD v2", "Premium LRS Provisioned Throughput (MBps)", "1/Hour", 0.0001, 125.0
                ),
            ],
            "NextPageLink": None,
        }
    ),
)


def azure_scenario(name: str, calls: Sequence[Call], answers: Sequence[Optional[bool]] = ()) -> DiskScenario:
    """Make an Azure scenario, whose disks are priced from the region's price list."""
    return DiskScenario(name, AZURE_SCOPE, None, calls, (AZURE_PRICES,), answers)


# ---------------------------------------------------------------- AWS answers


def volume(volume_id: str, kind: str, size: int, iops: Optional[int] = None, throughput: Optional[int] = None) -> Json:
    """Make an EBS volume as describe-volumes gives it."""
    record: Json = {
        "VolumeId": volume_id,
        "VolumeType": kind,
        "Size": size,
        "AvailabilityZone": "eu-west-1a",
        "State": "in-use",
        "MultiAttachEnabled": False,
    }
    if iops is not None:
        record["Iops"] = iops
    if throughput is not None:
        record["Throughput"] = throughput
    return record


def root_volume(number: int) -> Json:
    """Make instance i-0<number>'s root volume."""
    return volume(f"vol-root-0{number}", "gp3", 8, 3000, 125)


def region_reads(region: str, instances: Sequence[Json], volumes: Sequence[Json]) -> List[Call]:
    """Make the region's describe-instances and describe-volumes answers."""
    reservations = [{"ReservationId": f"r-0{index}", "Instances": [item]} for index, item in enumerate(instances)]
    return [
        aws_call(instances_args(region), {"Reservations": reservations}),
        aws_call(["ec2", "describe-volumes", "--region", region], {"Volumes": list(volumes)}),
    ]


def instance_type(name: str, nvme: str, baseline_iops: int, baseline_mbps: float) -> Json:
    """Make an instance type as describe-instance-types gives it, with its EBS limits."""
    ebs: Json = {
        "EbsOptimizedSupport": "default",
        "EncryptionSupport": "supported",
        "NvmeSupport": nvme,
        "EbsOptimizedInfo": {"BaselineIops": baseline_iops, "BaselineThroughputInMBps": baseline_mbps},
    }
    # Nitro types list their shared attachment limit; older types list none.
    if nvme == "required":
        ebs.update({"MaximumEbsAttachments": 28, "AttachmentLimitType": "shared"})
    return {
        "InstanceType": name,
        "VCpuInfo": {"DefaultVCpus": 8},
        "MemoryInfo": {"SizeInMiB": 32768},
        "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
        "BurstablePerformanceSupported": False,
        "EbsInfo": ebs,
        "NetworkInfo": {"MaximumNetworkInterfaces": 4, "Ipv4AddressesPerInterface": 15},
    }


NITRO_TYPE = instance_type("m6i.2xlarge", "required", 12000, 312.5)
XEN_TYPE = instance_type("m4.xlarge", "unsupported", 6000, 93.75)


def types_call(region: str = EU) -> Call:
    """Make the region's describe-instance-types answer: one Nitro type and one Xen type."""
    return aws_call(["ec2", "describe-instance-types", "--region", region], {"InstanceTypes": [NITRO_TYPE, XEN_TYPE]})


# The aws arguments that read which instances of the region have their Systems Manager agent online.
SSM_ONLINE_ARGS = [
    "ssm",
    "describe-instance-information",
    "--filters",
    "Key=PingStatus,Values=Online",
    "Key=ResourceType,Values=EC2Instance",
    "--region",
    EU,
]


def ssm_online(online: Sequence[str]) -> Call:
    """Make the region's answer of the instances whose Systems Manager agent is online."""
    listed = [{"InstanceId": item, "PingStatus": "Online", "ResourceType": "EC2Instance"} for item in online]
    return aws_call(SSM_ONLINE_ARGS, {"InstanceInformationList": listed})


def ssm_send(instance_id: str) -> Call:
    """Make Systems Manager's answer to the command sent to one instance: its command id."""
    parameters = {"commands": GUEST_SCRIPT[AWS].splitlines(), "executionTimeout": ["120"]}
    send = ["ssm", "send-command", "--instance-ids", instance_id, "--document-name", "AWS-RunShellScript"]
    send += ["--comment", "Zoorik disk assessment: reads disk usage", "--timeout-seconds", "300"]
    send += ["--parameters", json.dumps(parameters), "--region", EU]
    return aws_call(send, {"Command": {"CommandId": f"command-{instance_id}", "Status": "Pending"}})


def ssm_poll(instance_id: str) -> List[str]:
    """Give the aws arguments that read where the command sent to one instance stands."""
    command = f"command-{instance_id}"
    return ["ssm", "get-command-invocation", "--command-id", command, "--instance-id", instance_id, "--region", EU]


def invocation(status: str, printed: str = "") -> Json:
    """Make get-command-invocation's answer: the command's status, and what it printed."""
    return {"Status": status, "StatusDetails": status, "StandardOutputContent": printed}


def ssm_read(instance_id: str, printed: str) -> List[Call]:
    """Make Systems Manager's answers to the command sent to one instance: in progress at first, then its output."""
    return [
        ssm_send(instance_id),
        aws_call(ssm_poll(instance_id), invocation("InProgress")),
        aws_call(ssm_poll(instance_id), invocation("Success", printed)),
    ]


def cloudwatch(volume_id: str, per_minute: Tuple[float, float, float, float]) -> List[Call]:
    """Make CloudWatch's answers for one volume: one hour of each metric, its busiest minute's count."""
    calls = []
    for (metric, _, _), figure in zip(EBS_METRICS, per_minute):
        args = ["cloudwatch", "get-metric-statistics", "--namespace", "AWS/EBS", "--metric-name", metric]
        args += ["--dimensions", f"Name=VolumeId,Value={volume_id}", "--start-time", START, "--end-time", END]
        args += ["--period", "3600", "--statistics", "Maximum", "--region", EU]
        point = {"Timestamp": CLOUDWATCH_HOUR, "Maximum": figure, "Unit": "Count" if "Ops" in metric else "Bytes"}
        calls.append(aws_call(args, {"Label": metric, "Datapoints": [point]}))
    return calls


def product(kind: str, usage: str, unit: str, price: str) -> str:
    """Make one Price List product with its one on-demand price, as the text the Price List gives."""
    dimension = {"unit": unit, "pricePerUnit": {"USD": price}, "beginRange": "0", "endRange": "Inf"}
    attributes = {"volumeApiName": kind, "usagetype": usage, "regionCode": EU}
    terms = {"OnDemand": {"term-one": {"priceDimensions": {"dimension-one": dimension}}}}
    return json.dumps({"product": {"attributes": attributes}, "terms": terms})


# The region's EBS prices a month: each type's GB; gp3's IOPS above 3,000 and MB/s above 125 (40.96 a GiB/s is 0.04
# a MiB/s); io1's every IOPS.
EBS_PRODUCTS = {
    "Storage": [
        product("gp3", "EBS:VolumeUsage.gp3", "GB-Mo", "0.0800000000"),
        product("gp2", "EBS:VolumeUsage.gp2", "GB-Mo", "0.1000000000"),
        product("io1", "EBS:VolumeUsage.piops", "GB-Mo", "0.1250000000"),
    ],
    "System Operation": [
        product("gp3", "EBS:VolumeP-IOPS.gp3", "IOPS-Mo", "0.0050000000"),
        product("io1", "EBS:VolumeP-IOPS.piops", "IOPS-Mo", "0.0600000000"),
    ],
    "Provisioned Throughput": [product("gp3", "EBS:VolumeP-Throughput.gp3", "GiBps-mo", "40.9600000000")],
}


def ebs_prices() -> List[Call]:
    """Make the Price List's answers for the region's EBS prices, one call for each product family."""
    calls = []
    for family, products in EBS_PRODUCTS.items():
        terms = [("regionCode", EU), ("productFamily", family)]
        filters = json.dumps([{"Type": "TERM_MATCH", "Field": field, "Value": value} for field, value in terms])
        args = ["pricing", "get-products", "--service-code", "AmazonEC2", "--format-version", "aws_v1"]
        calls.append(aws_call([*args, "--filters", filters, "--region", "us-east-1"], {"PriceList": products}))
    return calls


def aws_scenario(name: str, calls: Sequence[Call], answers: Sequence[Optional[bool]] = ()) -> DiskScenario:
    """Make an AWS scenario in one region."""
    return DiskScenario(name, None, AWS_SCOPE, calls, (), answers)


# ---------------------------------------------------------------- Azure scenarios


def linux_vm() -> DiskScenario:
    """A Linux VM in zone 1 with a P30 and a P20 data disk, read after a yes: its pool goes on Premium SSD v2.

    Used: 200 + 50 = 250 GiB, so the pool holds 300 GiB: five 64 GiB members and the 8 GiB small one, 328 GB. The
    peak (600 IOPS, 34 MB/s) is below each member's free 3,000 IOPS and 125 MB/s, so each costs its GiB x 0.0001 x
    730 hours: 4.67 for 64 GiB and 0.58 for 8 GiB, 23.93 in all. The disks cost 130 + 70 = 200, so the saving is
    176.07 a month.
    """
    vm = vm_row("vm-one", [attached(0, "data-one", 1024), attached(1, "data-two", 512)])
    disks = [disk_row("data-one", 1024, "P30"), disk_row("data-two", 512, "P20")]
    printed = guest_output("L", [("0", ["/data"]), ("1", ["/logs"])], {"/data": 200, "/logs": 50}, AZURE_KERNEL)
    calls = [
        *azure_inventory([vm], disks),
        skus(),
        run_command("vm-one", printed),
        monitor("data-one", [hour(HOUR_ONE, (300, 200), (20e6, 10e6)), hour(HOUR_TWO, (100, 100), (5e6, 5e6))]),
        monitor("data-two", [hour(HOUR_ONE, (50, 50), (2e6, 2e6)), hour(HOUR_TWO, (20, 20), (1e6, 1e6))]),
    ]
    return azure_scenario("linux-vm", calls, (True,))


def shared_disk() -> DiskScenario:
    """A P20 shared by two VMs in no zone, one of which also has a P30 of its own.

    The shared disk stays as it is: the VM with only it has no pool, and the other VM's pool takes only its own disk.
    That pool goes on Premium SSD (Premium SSD v2 is not offered to a VM in no zone here): 100 GiB used needs 150, so
    three 128 GiB members at P10's 20, 60 in all, against the P30's 130, a saving of 70. The disks cost 70 + 130 =
    200 today: the shared disk counts once.
    """
    shared = attached(0, "shared-one", 512)
    vms = [
        vm_row("vm-one", [shared, attached(1, "data-one", 1024)], zones=None),
        vm_row("vm-two", [shared], zones=None),
    ]
    disks = [disk_row("data-one", 1024, "P30"), disk_row("shared-one", 512, "P20", shares=2)]
    one = guest_output("L", [("0", ["/shared"]), ("1", ["/data"])], {"/shared": 300, "/data": 100}, AZURE_KERNEL)
    two = guest_output("L", [("0", ["/shared"])], {"/shared": 300}, AZURE_KERNEL)
    calls = [
        *azure_inventory(vms, disks),
        skus(),
        run_command("vm-one", one),
        run_command("vm-two", two),
        monitor("data-one", [hour(HOUR_ONE, (300, 200), (20e6, 10e6))]),
    ]
    return azure_scenario("shared-disk", calls, (True,))


def priced_and_unpriced() -> DiskScenario:
    """A VM with a P30, priced, and a Standard SSD, which the price list doesn't price: its cost today is the P30's."""
    vm = vm_row("vm-one", [attached(0, "data-one", 1024), attached(1, "data-two", 1024, "StandardSSD_LRS")])
    disks = [disk_row("data-one", 1024, "P30"), disk_row("data-two", 1024, "E30", sku="StandardSSD_LRS")]
    printed = guest_output("L", [("0", ["/data"]), ("1", ["/archive"])], {"/data": 100, "/archive": 300}, AZURE_KERNEL)
    calls = [
        *azure_inventory([vm], disks),
        skus(),
        run_command("vm-one", printed),
        monitor("data-one", [hour(HOUR_ONE, (300, 200), (20e6, 10e6))]),
        monitor("data-two", [hour(HOUR_ONE, (20, 10), (1e6, 1e6))]),
    ]
    return azure_scenario("priced-and-unpriced", calls, (True,))


def windows() -> DiskScenario:
    """A Windows VM with a data disk: not estimated, and not read inside, so nothing is asked."""
    vm = vm_row("vm-windows", [attached(0, "data-one", 512)], osType="Windows")
    return azure_scenario("windows", [*azure_inventory([vm], [disk_row("data-one", 512, "P20")]), skus()])


def managed_group() -> DiskScenario:
    """A VM in a resource group that another Azure service (a Databricks workspace) runs: left to that service."""
    group = "databricks-group-one"
    vm = vm_row("vm-workers", [attached(0, "data-one", 512)], resourceGroup=group)
    managed: Json = {
        "subscriptionId": SUB,
        "name": group,
        "managedBy": f"/subscriptions/{SUB}/resourceGroups/{GROUP}/providers/Microsoft.Databricks/workspaces/space-one",
    }
    calls = [*azure_inventory([vm], [disk_row("data-one", 512, "P20")], [managed]), skus()]
    return azure_scenario("managed-group", calls)


def aks_node() -> DiskScenario:
    """A node VM of an AKS cluster, in the group the cluster runs: left out. A stopped VM beside it stays, unread."""
    group = "mc_group-one_cluster-one_westeurope"
    node = vm_row("vm-node", [attached(0, "data-node", 512)], resourceGroup=group)
    stopped = vm_row("vm-one", [attached(0, "data-one", 512)], power="PowerState/deallocated")
    cluster = f"/subscriptions/{SUB}/resourcegroups/{GROUP}/providers/Microsoft.ContainerService/managedClusters/one"
    managed: Json = {"subscriptionId": SUB, "name": group.upper(), "managedBy": cluster}
    disks = [disk_row("data-node", 512, "P20"), disk_row("data-one", 512, "P20")]
    return azure_scenario("aks-node", [*azure_inventory([node, stopped], disks, [managed]), skus()])


def managed_groups_failed() -> DiskScenario:
    """The read of managed resource groups is refused: no VM can be told apart from a cluster node, so none is assessed."""
    vms = [vm_row("vm-one", [attached(0, "data-one", 512)]), vm_row("vm-two", [attached(0, "data-two", 512)])]
    disks = [disk_row("data-one", 512, "P20"), disk_row("data-two", 512, "P20")]
    refused = (
        "ERROR: (AuthorizationFailed) The client does not have authorization to perform action "
        "'Microsoft.ResourceGraph/resources/read' over scope '/subscriptions/sub-one'.\n"
    )
    calls = [
        *azure_inventory(vms, disks)[:3],
        az_failure(graph_args(azure_cloud.MANAGED_GROUPS_QUERY), refused),
    ]
    return DiskScenario("managed-groups-failed", AZURE_SCOPE, None, calls, (), ())


def missing_disk_record() -> DiskScenario:
    """A VM whose second managed disk has no record: named, and left out of the figures. Its pool takes the P30.

    Used: 100 GiB, so the pool holds 150 GiB: three 64 GiB members and the 8 GiB small one on Premium SSD v2, 200 GB,
    at 3 x 4.67 + 0.58 = 14.59, against the P30's 130: a saving of 115.41.
    """
    vm = vm_row("vm-one", [attached(0, "data-one", 1024), attached(1, "data-gone", 512)])
    printed = guest_output("L", [("0", ["/data"]), ("1", [])], {"/data": 100}, AZURE_KERNEL)
    calls = [
        *azure_inventory([vm], [disk_row("data-one", 1024, "P30")]),
        skus(),
        run_command("vm-one", printed),
        monitor("data-one", [hour(HOUR_ONE, (300, 200), (20e6, 10e6))]),
    ]
    return azure_scenario("missing-disk-record", calls, (True,))


def too_few_slots() -> DiskScenario:
    """A VM whose size takes 4 data disks: a pool needs 8 slots or more, so it is not read inside."""
    vm = vm_row("vm-small", [attached(0, "data-one", 512)], size="Standard_D2s_v5")
    return azure_scenario("too-few-slots", [*azure_inventory([vm], [disk_row("data-one", 512, "P20")]), skus(D2S)])


def declined() -> DiskScenario:
    """A Linux VM with a data disk, and "no" at the prompt: nothing is read inside it."""
    vm = vm_row("vm-one", [attached(0, "data-one", 1024)])
    return azure_scenario("declined", [*azure_inventory([vm], [disk_row("data-one", 1024, "P30")]), skus()], (False,))


def hour_without_maximum() -> DiskScenario:
    """A disk with an hour that has no Maximum, only an Average far above the other hour: that hour is left out.

    The pool is sized on the other hour: the VM is in no zone, so on Premium SSD, three 128 GiB members at P10's 20,
    60 in all, against the P30's 130: a saving of 70.
    """
    vm = vm_row("vm-one", [attached(0, "data-one", 1024)], zones=None)
    printed = guest_output("L", [("0", ["/data"])], {"/data": 100}, AZURE_KERNEL)
    busy = hour(HOUR_TWO, (20000, 20000), (400e6, 400e6), field="average")
    calls = [
        *azure_inventory([vm], [disk_row("data-one", 1024, "P30")]),
        skus(),
        run_command("vm-one", printed),
        monitor("data-one", [hour(HOUR_ONE, (300, 200), (20e6, 10e6)), busy]),
    ]
    return azure_scenario("hour-without-maximum", calls, (True,))


def malformed_vm() -> DiskScenario:
    """Two VMs read after a yes; one's Run Command answer has no [stdout] part, so only that VM is not read."""
    vms = [vm_row("vm-one", [attached(0, "data-one", 1024)]), vm_row("vm-two", [attached(0, "data-two", 1024)])]
    disks = [disk_row("data-one", 1024, "P30"), disk_row("data-two", 1024, "P30")]
    printed = guest_output("L", [("0", ["/data"])], {"/data": 100}, AZURE_KERNEL)
    status = {"code": "ProvisioningState/succeeded", "level": "Info", "message": "Enable succeeded: no output"}
    calls = [
        *azure_inventory(vms, disks),
        skus(),
        run_command("vm-one", printed),
        az_call(run_command_args("vm-two"), {"value": [status]}),
        monitor("data-one", [hour(HOUR_ONE, (300, 200), (20e6, 10e6))]),
    ]
    return azure_scenario("malformed-vm", calls, (True,))


# ---------------------------------------------------------------- AWS scenarios


def eks_node_tags() -> DiskScenario:
    """An EKS node for each tag that names its cluster: all left out. A stopped instance beside them stays, unread."""
    eks = [
        instance(1, "m6i.2xlarge", [("/dev/sdf", "vol-01")], {"eks:cluster-name": "cluster-one"}),
        instance(2, "m6i.2xlarge", [("/dev/sdf", "vol-02")], {"eks:eks-cluster-name": "cluster-one"}),
        instance(3, "m6i.2xlarge", [("/dev/sdf", "vol-03")], {"aws:eks:cluster-name": "cluster-one"}),
        instance(4, "m6i.2xlarge", [("/dev/sdf", "vol-04")], {"kubernetes.io/cluster/cluster-one": "owned"}),
    ]
    stopped = {**instance(5, "m6i.2xlarge", [("/dev/sdf", "vol-05")]), "State": {"Code": 80, "Name": "stopped"}}
    volumes = [volume(f"vol-0{number}", "gp2", 500, 1500) for number in range(1, 6)]
    calls = [
        *region_reads(EU, [*eks, stopped], volumes),
        cluster_list(["cluster-one"]),
        types_call(),
        ssm_online([]),
        *ebs_prices(),
    ]
    return aws_scenario("eks-node-tags", calls)


def self_managed_node() -> DiskScenario:
    """A node of a self-managed cluster that isn't EKS: kept, read after a yes, and its pool goes on gp3.

    Used: 100 GiB, held at 115 GiB (103 GiB with btrfs's 3%, at most 90% full), on two gp3 volumes of 58 GiB at their
    free 3,000 IOPS and 125 MiB/s: 2 x 58 x 0.08 = 9.28, against the gp2's 500 x 0.10 = 50: a saving of 40.72.
    """
    node = instance(1, "m6i.2xlarge", [("/dev/sdf", "vol-01")], {"kubernetes.io/cluster/cluster-own": "owned"})
    printed = guest_output("V", [("vol-01", ["/data"])], {"/data": 100}, AWS_KERNEL)
    calls = [
        *region_reads(EU, [node], [root_volume(1), volume("vol-01", "gp2", 500, 1500)]),
        cluster_list(["cluster-one"]),
        types_call(),
        ssm_online(["i-01"]),
        *ssm_read("i-01", printed),
        *cloudwatch("vol-01", (6000, 6000, 600e6, 300e6)),
        *ebs_prices(),
    ]
    return aws_scenario("self-managed-node", calls, (True,))


def eks_list_failed() -> DiskScenario:
    """The EKS cluster list of one region is refused: its instance tagged as a cluster's node is named, not assessed."""
    node = instance(1, "m6i.2xlarge", [("/dev/sdf", "vol-01")], {"kubernetes.io/cluster/cluster-two": "owned"})
    refused = (
        "An error occurred (AccessDeniedException) when calling the ListClusters operation: User is not authorized "
        "to perform: eks:ListClusters\n"
    )
    calls = [
        *region_reads(EU, [], []),
        *region_reads(US, [node], [root_volume(1), volume("vol-01", "gp2", 500, 1500)]),
        aws_failure(["eks", "list-clusters", "--region", US], refused),
    ]
    return DiskScenario("eks-list-failed", None, AwsScope(ACCOUNT, (EU, US)), calls, (), ())


def no_terminal() -> DiskScenario:
    """An instance Systems Manager reaches, with no terminal to ask on: nothing is read inside it."""
    item = instance(1, "m6i.2xlarge", [("/dev/sdf", "vol-01")])
    calls = [
        *region_reads(EU, [item], [root_volume(1), volume("vol-01", "gp2", 500, 1500)]),
        types_call(),
        ssm_online(["i-01"]),
        *ebs_prices(),
    ]
    return aws_scenario("no-terminal", calls, (None,))


def ebs_types() -> DiskScenario:
    """An instance with a gp2, a gp3 and an io1 data volume, read after a yes: its pool goes on gp3.

    Used: 100 + 200 + 140 = 440 GiB, held at 504 GiB, on two gp3 volumes of 252 GiB. The peak of 255 MB/s
    (243.2 MiB/s) x 1.3 shared by two is 159 MiB/s each, held to 156 by the type's 312 MiB/s; at 3,000 IOPS each
    costs 252 x 0.08 + 31 x 0.04 = 21.40, 42.80 in all. The volumes cost 50 + 80 + 50 + 4,000 x 0.06 = 420, so the
    saving is 377.20.
    """
    item = instance(1, "m6i.2xlarge", [("/dev/sdf", "vol-01"), ("/dev/sdg", "vol-02"), ("/dev/sdh", "vol-03")])
    volumes = [
        root_volume(1),
        volume("vol-01", "gp2", 500, 1500),
        volume("vol-02", "gp3", 1000, 3000, 125),
        volume("vol-03", "io1", 400, 4000),
    ]
    used = {"/data": 100, "/logs": 200, "/db": 140}
    printed = guest_output("V", [("vol-01", ["/data"]), ("vol-02", ["/logs"]), ("vol-03", ["/db"])], used, AWS_KERNEL)
    calls = [
        *region_reads(EU, [item], volumes),
        types_call(),
        ssm_online(["i-01"]),
        *ssm_read("i-01", printed),
        *cloudwatch("vol-01", (12000, 6000, 1.2e9, 0.6e9)),
        *cloudwatch("vol-02", (30000, 30000, 3e9, 1.5e9)),
        *cloudwatch("vol-03", (120000, 60000, 6e9, 3e9)),
        *ebs_prices(),
    ]
    return aws_scenario("ebs-types", calls, (True,))


def xen_type() -> DiskScenario:
    """An instance of a Xen type: a pool needs a Nitro type, so it is not read inside."""
    item = instance(1, "m4.xlarge", [("/dev/sdf", "vol-01")])
    calls = [
        *region_reads(EU, [item], [root_volume(1), volume("vol-01", "gp2", 500, 1500)]),
        types_call(),
        ssm_online(["i-01"]),
        *ebs_prices(),
    ]
    return aws_scenario("xen-type", calls)


def no_ssm() -> DiskScenario:
    """An instance Systems Manager doesn't manage: the run can't read inside it, so nothing is asked."""
    item = instance(1, "m6i.2xlarge", [("/dev/sdf", "vol-01")])
    calls = [
        *region_reads(EU, [item], [root_volume(1), volume("vol-01", "gp2", 500, 1500)]),
        types_call(),
        ssm_online([]),
        *ebs_prices(),
    ]
    return aws_scenario("no-ssm", calls)


SCENARIOS = (
    linux_vm(),
    shared_disk(),
    priced_and_unpriced(),
    windows(),
    managed_group(),
    aks_node(),
    eks_node_tags(),
    self_managed_node(),
    eks_list_failed(),
    managed_groups_failed(),
    missing_disk_record(),
    too_few_slots(),
    declined(),
    no_terminal(),
    ebs_types(),
    xen_type(),
    no_ssm(),
    hour_without_maximum(),
    malformed_vm(),
)
