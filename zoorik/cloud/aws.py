"""AWS through aws: the account, EKS, EC2, CloudWatch, Systems Manager, the Price List and spot price history.

AwsCli is the only class that builds aws commands. Every call names its region, so no region is taken from the
environment or a profile. Each answer is parsed into the dataclasses below, with a field Optional only where AWS
documents that it may leave it out.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from zoorik.cloud.cli import CloudCli, Host, Reason, Tool, Window, kubeconfig_file, utc_text
from zoorik.cloud.fields import Record, parse_json, texts_of
from zoorik.cloud.started import StartedCommands
from zoorik.errors import (
    AccessDenied,
    AssessmentError,
    CommandFailed,
    CommandTimedOut,
    NotSignedIn,
    Throttled,
    UnexpectedResponse,
)
from zoorik.system.memo import Memo

# Systems Manager answers so while a command it was sent is still on its way to the instance.
NOT_DELIVERED = Reason(r"invocationdoesnotexist", CommandFailed, "the command has not reached the instance yet")

# Rows are tried in order and the first match wins, so the specific rows come before the general ones. Throttling
# comes first: a throttled call is tried again whatever else its error says.
AWS_REASONS = (
    Reason(
        r"requestlimitexceeded|throttl|rate exceeded|too ?many ?requests|\b429\b",
        Throttled,
        "AWS is throttling requests; try again later",
    ),
    Reason(r"ssm:sendcommand", AccessDenied, "no permission to use Systems Manager Run Command (ssm:SendCommand)"),
    Reason(
        r"ssm:getcommandinvocation",
        AccessDenied,
        "no permission to read Run Command results (ssm:GetCommandInvocation)",
    ),
    NOT_DELIVERED,
    Reason(r"invalidinstanceid", CommandFailed, "Systems Manager cannot reach this instance"),
    Reason(r"unable to locate credentials", NotSignedIn, "no AWS credentials; run aws configure or aws sso login"),
    Reason(r"token has expired", NotSignedIn, "AWS sign-in has expired; run aws sso login"),
    Reason(r"expiredtoken", NotSignedIn, "AWS credentials have expired"),
    Reason(r"accessdenied|unauthorizedoperation", AccessDenied, "access denied"),
    Reason(r"optinrequired", CommandFailed, "region not enabled for this account"),
    Reason(r"timed out", CommandTimedOut, "no answer in time"),
)

AWS = Tool(
    cloud="AWS",
    output=("--output", "json"),
    # The pager would wait for a key press on long answers.
    overrides={"AWS_PAGER": ""},
    reasons=AWS_REASONS,
    cmd_script=False,
)

# Every account has us-east-1, so the account-wide calls go there.
ACCOUNT_REGION = "us-east-1"
# The Price List API answers in us-east-1.
PRICE_LIST_REGION = "us-east-1"
# A region's whole price list, and its whole list of instance types, take minutes to page through.
PRICE_LIST_TIMEOUT = 300.0
INSTANCE_TYPES_TIMEOUT = 300.0
# The instance states in which an instance holds its EBS volumes.
LIVE_STATES = "Name=instance-state-name,Values=pending,running,stopping,stopped"
SSM_DOCUMENT = "AWS-RunShellScript"
SSM_COMMENT = "Zoorik disk assessment: reads disk usage"
# How long the script may run on the instance, and how long Systems Manager may take to deliver it.
SSM_EXECUTION_SECONDS = 120
SSM_DELIVERY_SECONDS = 300
# CloudWatch figures are read per hour.
CLOUDWATCH_PERIOD = 3600
SPOT_PRODUCT = "Linux/UNIX"
# A Nitro instance type that lists no attachment limit of its own shares 28 among its network interfaces, EBS volumes
# and NVMe instance store volumes.
NITRO_ATTACHMENTS = 28


@dataclass(frozen=True)
class AwsScope:
    """What the run reads of AWS: the signed-in account and its enabled regions."""

    account: str
    regions: Tuple[str, ...]


@dataclass(frozen=True)
class EksCluster:
    """One EKS cluster's record."""

    region: str
    name: str
    status: str
    # resourcesVpcConfig: whether the API server answers from the internet.
    public_endpoint: bool


@dataclass(frozen=True)
class NodeGroup:
    """One managed node group of an EKS cluster."""

    name: str
    # ON_DEMAND, SPOT or CAPACITY_BLOCK.
    capacity_type: str


@dataclass(frozen=True)
class AttachedVolume:
    """An EBS volume attached to an instance, and the device name it is attached at."""

    device: str
    volume: str


@dataclass(frozen=True)
class Ec2Instance:
    """One EC2 instance."""

    id: str
    instance_type: str
    state: str
    # windows for a Windows instance; AWS leaves it out for the others.
    platform: Optional[str]
    # None for an untagged instance.
    tags: Optional[Mapping[str, str]]
    # spot or scheduled; AWS leaves it out for an on-demand instance.
    lifecycle: Optional[str]
    root_device: str
    volumes: Tuple[AttachedVolume, ...]
    interfaces: int


@dataclass(frozen=True)
class EbsVolume:
    """One EBS volume."""

    id: str
    volume_type: str
    size_gib: int
    # AWS leaves the IOPS out for magnetic, st1 and sc1 volumes, and gives the throughput (MiB/s) for gp3 only.
    iops: Optional[int]
    throughput: Optional[int]
    multi_attach: bool


@dataclass(frozen=True)
class InstanceType:
    """One EC2 instance type: what it holds, and its EBS and network limits."""

    name: str
    vcpus: int
    memory_mib: int
    architectures: Tuple[str, ...]
    burstable: bool
    gpus: int
    # EbsInfo: AWS gives the attachment limit and its kind (shared or dedicated) for some types only.
    max_attachments: Optional[int]
    attachment_limit: Optional[str]
    ebs_nvme: str
    # EbsOptimizedInfo, for EBS-optimized types only.
    baseline_iops: Optional[int]
    baseline_mbps: Optional[float]
    # InstanceStorageInfo, for types with instance storage only.
    store_disks: int
    store_nvme: Optional[str]
    max_interfaces: int
    ipv4_per_interface: int


@dataclass(frozen=True)
class Invocation:
    """Where a Systems Manager command stands on one instance, and what it printed."""

    status: str
    details: str
    stdout: str


@dataclass(frozen=True)
class Datapoint:
    """One hour of one CloudWatch metric: its Maximum."""

    stamp: str
    maximum: float


@dataclass(frozen=True)
class PriceDimension:
    """One on-demand price of a Price List product: US dollars per unit."""

    usd: float
    unit: str


@dataclass(frozen=True)
class PriceProduct:
    """One product of the AWS Price List, with its on-demand prices."""

    attributes: Mapping[str, str]
    on_demand: Tuple[PriceDimension, ...]

    def attribute(self, name: str) -> str:
        """Read a required attribute."""
        value = self.attributes.get(name)
        if value is None:
            raise UnexpectedResponse(f"unexpected answer: a Price List product has no {name}")
        return value


@dataclass(frozen=True)
class SpotPrice:
    """One spot price of a machine type in one zone, and when it took effect."""

    zone: str
    price: float
    stamp: str


class AwsCli:
    """AWS, read through aws."""

    cloud = "AWS"

    def __init__(self, host: Host, started: StartedCommands) -> None:
        """Run aws on host; keep each Systems Manager start in started."""
        self._cli = CloudCli(AWS, host)
        self._temp_root = host.temp_root
        self._started = started
        self._cluster_names: Memo[str, Tuple[str, ...]] = Memo(self._read_cluster_names)
        self._instances: Memo[str, Tuple[Ec2Instance, ...]] = Memo(self._read_instances)
        self._instance_types: Memo[str, Dict[str, InstanceType]] = Memo(self._read_instance_types)
        self._type_zones: Memo[str, Dict[str, Tuple[str, ...]]] = Memo(self._read_type_zones)

    def scope(self) -> AwsScope:
        """Read the signed-in account and its enabled regions; a region list that can't be read fails the scope."""
        answer = self._cli.read(["aws", "sts", "get-caller-identity", "--region", ACCOUNT_REGION])
        account = Record(answer, "aws sts get-caller-identity").text("Account")
        argv = ["aws", "ec2", "describe-regions", "--query", "Regions[].RegionName", "--region", ACCOUNT_REGION]
        try:
            regions = texts_of(self._cli.read(argv), "aws ec2 describe-regions")
        except AssessmentError as error:
            raise type(error)(f"could not list the account's regions: {error}") from error
        return AwsScope(account, tuple(sorted(regions)))

    def cluster_names(self, region: str) -> Tuple[str, ...]:
        """Name the EKS clusters of a region; read once per run, for both parts."""
        return self._cluster_names.get(region)

    def cluster(self, region: str, name: str) -> EksCluster:
        """Read one EKS cluster's record."""
        answer = self._cli.read(["aws", "eks", "describe-cluster", "--name", name, "--region", region])
        record = Record(answer, "aws eks describe-cluster").record("cluster")
        network = record.record("resourcesVpcConfig")
        return EksCluster(
            region=region,
            name=record.text("name"),
            status=record.text("status"),
            public_endpoint=network.flag("endpointPublicAccess"),
        )

    def node_groups(self, region: str, cluster: str) -> List[NodeGroup]:
        """Read the managed node groups of one EKS cluster."""
        argv = ["aws", "eks", "list-nodegroups", "--cluster-name", cluster, "--region", region]
        names = Record(self._cli.read(argv), "aws eks list-nodegroups").texts("nodegroups")
        return [self._node_group(region, cluster, name) for name in sorted(names)]

    def write_kubeconfig(self, region: str, cluster: str) -> str:
        """Write the cluster's kubeconfig to the run's temporary folder, and give its path."""
        path = kubeconfig_file(self._temp_root, ("eks", region, cluster))
        self._cli.run(["aws", "eks", "update-kubeconfig", "--name", cluster, "--kubeconfig", path, "--region", region])
        return path

    def instances(self, region: str) -> Tuple[Ec2Instance, ...]:
        """Read the instances of a region that hold their volumes; read once per run, for both parts."""
        return self._instances.get(region)

    def volumes(self, region: str) -> List[EbsVolume]:
        """Read the EBS volumes of a region."""
        answer = self._cli.read(["aws", "ec2", "describe-volumes", "--region", region])
        return [_volume(volume) for volume in Record(answer, "aws ec2 describe-volumes").records("Volumes")]

    def instance_types(self, region: str) -> Mapping[str, InstanceType]:
        """Read every instance type of a region, by name; read once per run."""
        return self._instance_types.get(region)

    def type_zones(self, region: str) -> Mapping[str, Tuple[str, ...]]:
        """Read the zones of a region that offer each instance type; read once per run."""
        return self._type_zones.get(region)

    def ssm_online(self, region: str) -> FrozenSet[str]:
        """Name the instances of a region whose Systems Manager agent is online."""
        argv = [
            "aws",
            "ssm",
            "describe-instance-information",
            "--filters",
            "Key=PingStatus,Values=Online",
            "Key=ResourceType,Values=EC2Instance",
            "--region",
            region,
        ]
        listed = Record(self._cli.read(argv), "aws ssm describe-instance-information").records(
            "InstanceInformationList"
        )
        return frozenset(information.text("InstanceId") for information in listed)

    def send_command(self, region: str, instance: str, script: str) -> str:
        """Send script to run on the instance through Systems Manager Run Command, and give the command's id."""
        self._started.ssm(instance)
        parameters = {"commands": script.splitlines(), "executionTimeout": [str(SSM_EXECUTION_SECONDS)]}
        argv = [
            "aws",
            "ssm",
            "send-command",
            "--instance-ids",
            instance,
            "--document-name",
            SSM_DOCUMENT,
            "--comment",
            SSM_COMMENT,
            "--timeout-seconds",
            str(SSM_DELIVERY_SECONDS),
            "--parameters",
            json.dumps(parameters),
            "--region",
            region,
        ]
        return Record(self._cli.read(argv), "aws ssm send-command").record("Command").text("CommandId")

    def command_invocation(self, region: str, command: str, instance: str) -> Optional[Invocation]:
        """Read where a sent command stands on the instance; None while it has not reached the instance."""
        argv = ["aws", "ssm", "get-command-invocation", "--command-id", command, "--instance-id", instance]
        answer = self._cli.read_unless([*argv, "--region", region], NOT_DELIVERED)
        if answer is None:
            return None
        record = Record(answer, "aws ssm get-command-invocation")
        return Invocation(record.text("Status"), record.text("StatusDetails"), record.text("StandardOutputContent"))

    def volume_maxima(self, region: str, volume: str, metric: str, window: Window) -> List[Datapoint]:
        """Read the Maximum of each hour of the window, for one CloudWatch metric of an EBS volume."""
        argv = [
            "aws",
            "cloudwatch",
            "get-metric-statistics",
            "--namespace",
            "AWS/EBS",
            "--metric-name",
            metric,
            "--dimensions",
            f"Name=VolumeId,Value={volume}",
            "--start-time",
            utc_text(window.start),
            "--end-time",
            utc_text(window.end),
            "--period",
            str(CLOUDWATCH_PERIOD),
            "--statistics",
            "Maximum",
            "--region",
            region,
        ]
        points = Record(self._cli.read(argv), "aws cloudwatch get-metric-statistics").records("Datapoints")
        return [Datapoint(point.text("Timestamp"), point.number("Maximum")) for point in points]

    def price_products(self, terms: Sequence[Tuple[str, str]]) -> List[PriceProduct]:
        """Read the EC2 products of the Price List that match every term, a field and its value."""
        filters = json.dumps([{"Type": "TERM_MATCH", "Field": field, "Value": value} for field, value in terms])
        argv = [
            "aws",
            "pricing",
            "get-products",
            "--service-code",
            "AmazonEC2",
            "--format-version",
            "aws_v1",
            "--filters",
            filters,
            "--region",
            PRICE_LIST_REGION,
        ]
        listed = Record(self._cli.read(argv, PRICE_LIST_TIMEOUT), "aws pricing get-products").texts("PriceList")
        return [
            _product(Record(parse_json(item, "aws pricing get-products"), "a Price List product")) for item in listed
        ]

    def spot_prices(self, region: str, machine_type: str, at: datetime) -> List[SpotPrice]:
        """Read the Linux spot prices of a machine type in each zone of a region, in effect at the time given."""
        argv = [
            "aws",
            "ec2",
            "describe-spot-price-history",
            "--instance-types",
            machine_type,
            "--product-descriptions",
            SPOT_PRODUCT,
            "--start-time",
            utc_text(at),
            "--region",
            region,
        ]
        history = Record(self._cli.read(argv), "aws ec2 describe-spot-price-history").records("SpotPriceHistory")
        return [
            SpotPrice(entry.text("AvailabilityZone"), _decimal(entry, "SpotPrice"), entry.text("Timestamp"))
            for entry in history
        ]

    def _node_group(self, region: str, cluster: str, name: str) -> NodeGroup:
        """Read one managed node group's record."""
        argv = ["aws", "eks", "describe-nodegroup", "--cluster-name", cluster, "--nodegroup-name", name]
        answer = self._cli.read([*argv, "--region", region])
        record = Record(answer, "aws eks describe-nodegroup").record("nodegroup")
        return NodeGroup(name=record.text("nodegroupName"), capacity_type=record.text("capacityType"))

    def _read_cluster_names(self, region: str) -> Tuple[str, ...]:
        """Read the names of a region's EKS clusters."""
        answer = self._cli.read(["aws", "eks", "list-clusters", "--region", region])
        return tuple(sorted(Record(answer, "aws eks list-clusters").texts("clusters")))

    def _read_instances(self, region: str) -> Tuple[Ec2Instance, ...]:
        """Read the instances of a region that hold their volumes: pending, running, stopping or stopped."""
        answer = self._cli.read(["aws", "ec2", "describe-instances", "--filters", LIVE_STATES, "--region", region])
        return tuple(
            _instance(instance)
            for reservation in Record(answer, "aws ec2 describe-instances").records("Reservations")
            for instance in reservation.records("Instances")
        )

    def _read_instance_types(self, region: str) -> Dict[str, InstanceType]:
        """Read every instance type of a region."""
        answer = self._cli.read(["aws", "ec2", "describe-instance-types", "--region", region], INSTANCE_TYPES_TIMEOUT)
        types = [
            _instance_type(item) for item in Record(answer, "aws ec2 describe-instance-types").records("InstanceTypes")
        ]
        return {kind.name: kind for kind in types}

    def _read_type_zones(self, region: str) -> Dict[str, Tuple[str, ...]]:
        """Read which zones of a region offer each instance type."""
        argv = ["aws", "ec2", "describe-instance-type-offerings", "--location-type", "availability-zone"]
        answer = self._cli.read([*argv, "--region", region], INSTANCE_TYPES_TIMEOUT)
        zones: Dict[str, List[str]] = {}
        for offer in Record(answer, "aws ec2 describe-instance-type-offerings").records("InstanceTypeOfferings"):
            zones.setdefault(offer.text("InstanceType"), []).append(offer.text("Location"))
        return {kind: tuple(sorted(listed)) for kind, listed in zones.items()}


def _decimal(record: Record, key: str) -> float:
    """Read a number that AWS gives as text, such as a price."""
    text = record.text(key)
    try:
        return float(text)
    except ValueError:
        raise UnexpectedResponse(f"unexpected answer: {record.path}.{key} is not a number") from None


def _instance(record: Record) -> Ec2Instance:
    """Read one instance of aws ec2 describe-instances."""
    tags = record.optional_records("Tags")
    return Ec2Instance(
        id=record.text("InstanceId"),
        instance_type=record.text("InstanceType"),
        state=record.record("State").text("Name"),
        platform=record.optional_text("Platform"),
        tags=None if tags is None else {tag.text("Key"): tag.text("Value") for tag in tags},
        lifecycle=record.optional_text("InstanceLifecycle"),
        root_device=record.text("RootDeviceName"),
        volumes=tuple(
            AttachedVolume(mapping.text("DeviceName"), mapping.record("Ebs").text("VolumeId"))
            for mapping in record.records("BlockDeviceMappings")
        ),
        interfaces=len(record.records("NetworkInterfaces")),
    )


def _volume(record: Record) -> EbsVolume:
    """Read one volume of aws ec2 describe-volumes."""
    return EbsVolume(
        id=record.text("VolumeId"),
        volume_type=record.text("VolumeType"),
        size_gib=record.integer("Size"),
        iops=record.optional_integer("Iops"),
        throughput=record.optional_integer("Throughput"),
        multi_attach=record.flag("MultiAttachEnabled"),
    )


def _instance_type(record: Record) -> InstanceType:
    """Read one type of aws ec2 describe-instance-types."""
    gpu = record.optional_record("GpuInfo")
    ebs = record.record("EbsInfo")
    optimized = ebs.optional_record("EbsOptimizedInfo")
    storage = record.optional_record("InstanceStorageInfo")
    network = record.record("NetworkInfo")
    return InstanceType(
        name=record.text("InstanceType"),
        vcpus=record.record("VCpuInfo").integer("DefaultVCpus"),
        memory_mib=record.record("MemoryInfo").integer("SizeInMiB"),
        architectures=tuple(record.record("ProcessorInfo").texts("SupportedArchitectures")),
        burstable=record.flag("BurstablePerformanceSupported"),
        gpus=0 if gpu is None else sum(card.integer("Count") for card in gpu.records("Gpus")),
        max_attachments=ebs.optional_integer("MaximumEbsAttachments"),
        attachment_limit=ebs.optional_text("AttachmentLimitType"),
        ebs_nvme=ebs.text("NvmeSupport"),
        baseline_iops=None if optimized is None else optimized.integer("BaselineIops"),
        baseline_mbps=None if optimized is None else optimized.number("BaselineThroughputInMBps"),
        store_disks=0 if storage is None else sum(disk.integer("Count") for disk in storage.records("Disks")),
        store_nvme=None if storage is None else storage.text("NvmeSupport"),
        max_interfaces=network.integer("MaximumNetworkInterfaces"),
        ipv4_per_interface=network.integer("Ipv4AddressesPerInterface"),
    )


def _product(record: Record) -> PriceProduct:
    """Read one Price List product and its on-demand prices; a product sold only on other terms has none."""
    terms = record.record("terms").optional_keyed_records("OnDemand")
    dimensions = (
        ()
        if terms is None
        else tuple(_dimension(dimension) for term in terms for dimension in term.keyed_records("priceDimensions"))
    )
    return PriceProduct(record.record("product").text_map("attributes"), dimensions)


def _dimension(record: Record) -> PriceDimension:
    """Read one price of a Price List term."""
    return PriceDimension(usd=_decimal(record.record("pricePerUnit"), "USD"), unit=record.text("unit"))
