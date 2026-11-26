"""AWS's EC2 instances with data volumes, region by region, with each instance type's EBS limits.

The nodes of EKS clusters are left out: the Kubernetes part counts them, so each cost is counted once. A node of a
self-managed cluster that isn't EKS stays.
"""

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

from zoorik.cloud.aws import AwsCli, AwsScope, EbsVolume, Ec2Instance, InstanceType
from zoorik.cloud.keys import AWS
from zoorik.console import Progress
from zoorik.disks.facts import ebs_limits
from zoorik.disks.model import DataDisk, Disk, Inventory, NotAssessed, ScopeRead, Unread, Vm
from zoorik.errors import AssessmentError
from zoorik.system.workers import parallel

# EKS tags each node with one of these keys; a self-managed node carries kubernetes.io/cluster/<name> instead.
EKS_TAG_KEYS = ("eks:cluster-name", "eks:eks-cluster-name", "aws:eks:cluster-name")
CLUSTER_TAG = "kubernetes.io/cluster/"
NAME_TAG = "Name"
NO_VOLUME = "volume details not readable"
NO_SSM = "Systems Manager does not manage it (no SSM agent reporting)"


@dataclass(frozen=True)
class RegionReads:
    """What one region's reads give each of its instances: the account and region, the volumes, the instance types."""

    account: str
    region: str
    volumes: Mapping[str, EbsVolume]
    # The region's instance types by name, or why they couldn't be read.
    types: Union[Mapping[str, InstanceType], str]


@dataclass(frozen=True)
class RegionInventory:
    """One region's instances with data volumes, those it couldn't tell apart from EKS nodes, and what it held."""

    vms: Tuple[Vm, ...]
    not_assessed: Tuple[NotAssessed, ...]
    read: ScopeRead


class AwsInventory:
    """AWS's EC2 instances and EBS volumes in the account's enabled regions."""

    def __init__(self, cli: AwsCli, scope: AwsScope) -> None:
        """Read through cli, in scope's regions."""
        self._cli = cli
        self._scope = scope

    def read(self, progress: Progress) -> Inventory:
        """Read every instance with data volumes, a few regions at a time; a region that fails is named."""
        results = parallel(self._region, self._scope.regions, progress)
        regions = [result for result in results if isinstance(result, RegionInventory)]
        unread = tuple(
            Unread(AWS, region, str(result))
            for region, result in zip(self._scope.regions, results)
            if isinstance(result, AssessmentError)
        )
        return Inventory(
            vms=tuple(vm for region in regions for vm in region.vms),
            not_assessed=tuple(unsure for region in regions for unsure in region.not_assessed),
            read=tuple(region.read for region in regions),
            unread=unread,
            names={},
        )

    def _region(self, region: str) -> RegionInventory:
        """Read one region's instances and volumes, and what each instance type allows and Systems Manager reaches."""
        instances = self._cli.instances(region)
        volumes = {volume.id: volume for volume in self._cli.volumes(region)}
        kept, unsure = self._without_eks_nodes(region, [instance for instance in instances if has_data(instance)])
        reads = RegionReads(self._scope.account, region, volumes, self._types(region, kept))
        channels = self._channels(region, kept)
        vms = tuple(make_vm(reads, instance, channels.get(instance.id)) for instance in kept)
        return RegionInventory(vms, tuple(unsure), ScopeRead(region, len(instances), len(volumes)))

    def _without_eks_nodes(
        self, region: str, instances: Sequence[Ec2Instance]
    ) -> Tuple[List[Ec2Instance], List[NotAssessed]]:
        """Leave out the instances that are EKS nodes, and name those it can't tell apart from them."""
        kept: List[Ec2Instance] = []
        unsure: List[NotAssessed] = []
        for instance in instances:
            keys = () if instance.tags is None else tuple(instance.tags)
            if any(key in EKS_TAG_KEYS for key in keys):
                continue
            clusters = [key[len(CLUSTER_TAG) :] for key in keys if key.startswith(CLUSTER_TAG)]
            if not clusters:
                kept.append(instance)
                continue
            try:
                eks = self._cli.cluster_names(region)
            except AssessmentError as error:
                reason = f"could not tell whether they are EKS nodes: {error}"
                unsure.append(NotAssessed(AWS, self._scope.account, region, instance_name(instance), reason))
                continue
            if not any(cluster in eks for cluster in clusters):
                kept.append(instance)
        return kept, unsure

    def _types(self, region: str, instances: Sequence[Ec2Instance]) -> Union[Mapping[str, InstanceType], str]:
        """Give the region's instance types, or why they couldn't be read; read only when an instance needs them."""
        if not instances:
            return {}
        try:
            return self._cli.instance_types(region)
        except AssessmentError as error:
            return f"instance type limits not readable: {error}"

    def _channels(self, region: str, instances: Sequence[Ec2Instance]) -> Dict[str, str]:
        """Name why the run can't read inside each instance that Systems Manager doesn't reach."""
        if not any(linux(instance) for instance in instances):
            return {}
        try:
            online = self._cli.ssm_online(region)
        except AssessmentError as error:
            return {instance.id: f"Systems Manager not readable: {error}" for instance in instances}
        return {instance.id: NO_SSM for instance in instances if instance.id not in online}


def has_data(instance: Ec2Instance) -> bool:
    """Tell whether the instance has data volumes: an attached EBS volume besides its root volume."""
    return any(attached.device != instance.root_device for attached in instance.volumes)


def linux(instance: Ec2Instance) -> bool:
    """Tell whether the instance runs Linux: AWS names only Windows as a platform."""
    return instance.platform is None or instance.platform.lower() != "windows"


def instance_name(instance: Ec2Instance) -> str:
    """Name the instance by its Name tag, or by its id when it has none."""
    name = None if instance.tags is None else instance.tags.get(NAME_TAG)
    return name if name else instance.id


def make_vm(reads: RegionReads, instance: Ec2Instance, channel: Optional[str]) -> Vm:
    """Build one instance with its data volumes, its type's EBS limits and how the run reaches inside it."""
    data = data_disks(instance, reads.volumes, reads.region)
    kind = None if isinstance(reads.types, str) else reads.types.get(instance.instance_type)
    if isinstance(reads.types, str):
        facts_note = reads.types
    elif kind is None:
        facts_note = f"{instance.instance_type} not listed in {reads.region}"
    else:
        facts_note = ""
    return Vm(
        id=instance.id,
        name=instance_name(instance),
        cloud=AWS,
        account=reads.account,
        region=reads.region,
        zone=None,
        size=instance.instance_type,
        os_type="Linux" if linux(instance) else "Windows",
        power=instance.state.capitalize(),
        data=data,
        managed_by=None,
        channel=channel,
        limits=None if kind is None else ebs_limits(instance, data, kind),
        facts_note=facts_note,
    )


def data_disks(instance: Ec2Instance, volumes: Mapping[str, EbsVolume], region: str) -> Tuple[DataDisk, ...]:
    """Build the instance's data volumes in device-name order, each numbered by its place in that order."""
    attached = sorted(
        (volume for volume in instance.volumes if volume.device != instance.root_device),
        key=lambda volume: volume.device,
    )
    disks: List[DataDisk] = []
    for lun, volume in enumerate(attached):
        record = volumes.get(volume.volume)
        if record is None:
            disks.append(DataDisk(lun, None, None, NO_VOLUME, volume.device))
        else:
            disks.append(DataDisk(lun, record.size_gib, ebs_disk(record, region), "", volume.device))
    return tuple(disks)


def ebs_disk(volume: EbsVolume, region: str) -> Disk:
    """Build one EBS volume."""
    return Disk(
        id=volume.id,
        cloud=AWS,
        region=region,
        sku=volume.volume_type,
        size_gb=volume.size_gib,
        tier="",
        iops=volume.iops,
        mbps=volume.throughput,
        ade=False,
        shared=volume.multi_attach,
    )
