"""Every AKS and EKS cluster the signed-in clouds hold, and how each one is read, decided from its settings.

AKS, per enabled subscription: a stopped cluster is named. A cluster whose API server is private, or limited to
authorized IP ranges, is read through az aks command invoke; it is named instead when command invoke is turned off for
it, or under --no-invoke. Any other cluster is read with kubectl, through the kubeconfig az aks get-credentials writes
in the run's temporary folder; with Entra ID sign-in, kubelogin converts it first. EKS, per enabled region: an ACTIVE
cluster is read with kubectl through the kubeconfig aws eks update-kubeconfig writes; any other status is named, and so
is a cluster whose node groups can't be read. An EKS API server that doesn't answer is named; when its endpoint is
private only, the reason says to run this from inside its VPC.
"""

from dataclasses import dataclass
from typing import Callable, List, Mapping, Protocol, Tuple, Union

from zoorik.cloud.aws import AwsCli, AwsScope, EksCluster, NodeGroup
from zoorik.cloud.azure import AksCluster, AzureCli, AzureScope
from zoorik.cloud.keys import AWS, AZURE
from zoorik.console import Progress
from zoorik.errors import ApiUnreachable, AssessmentError
from zoorik.kubectl.api import KubeApi
from zoorik.kubectl.direct import UNREACHABLE, Kubeconfig
from zoorik.kubectl.usage import SampleWindow
from zoorik.kubernetes.collect import unmarked
from zoorik.kubernetes.model import ClusterId, ClusterRead, NotAssessed, Unread
from zoorik.system.workers import parallel

STOPPED = "Stopped"
ACTIVE = "ACTIVE"
STOPPED_REASON = "the cluster is stopped"
INVOKE_OFF = "command invoke is turned off for this cluster"
NO_INVOKE = (
    "its API server is private (or limited to authorized IP ranges); run without --no-invoke to read it through "
    "az aks command invoke"
)
PRIVATE_ENDPOINT = f"{UNREACHABLE}: its endpoint is private, so run this from inside its VPC"


@dataclass(frozen=True)
class Readers:
    """The two read methods, each made for one cluster."""

    direct: Callable[[Kubeconfig], KubeApi]
    invoke: Callable[[AksCluster], KubeApi]


class Target(Protocol):
    """A cluster to read, by the read method its settings chose."""

    @property
    def cluster(self) -> ClusterId:
        """Name the cluster."""

    def read(self, window: SampleWindow) -> ClusterRead:
        """Read the cluster, its usage sampled over window."""


@dataclass(frozen=True)
class Found:
    """A cloud's clusters: those to read, those named with the reason, and the scopes whose clusters weren't listed."""

    targets: Tuple[Target, ...]
    not_assessed: Tuple[NotAssessed, ...]
    unread: Tuple[Unread, ...]
    # How many subscriptions or regions listed no clusters.
    empty: int
    # Azure subscriptions' names by id.
    names: Mapping[str, str]


class ClusterSource(Protocol):
    """A cloud's clusters."""

    def clusters(self, progress: Progress) -> Found:
        """List the clusters, and choose how each is read, reporting progress."""


def aks_id(record: AksCluster) -> ClusterId:
    """Name an AKS cluster: its subscription, its region and its name."""
    return ClusterId(AZURE, record.subscription, record.location, record.name)


@dataclass(frozen=True)
class AksDirect:
    """An AKS cluster read with kubectl."""

    cli: AzureCli
    record: AksCluster
    readers: Readers

    @property
    def cluster(self) -> ClusterId:
        """Name the cluster."""
        return aks_id(self.record)

    def read(self, window: SampleWindow) -> ClusterRead:
        """Write the cluster's kubeconfig, then read the cluster with kubectl."""
        path = self.cli.write_kubeconfig(self.record)
        snapshot = self.readers.direct(Kubeconfig(path, self.record.entra_id)).read(window)
        return ClusterRead(snapshot, self.record.pools, (), ())


@dataclass(frozen=True)
class AksInvoke:
    """An AKS cluster read through az aks command invoke."""

    record: AksCluster
    readers: Readers

    @property
    def cluster(self) -> ClusterId:
        """Name the cluster."""
        return aks_id(self.record)

    def read(self, window: SampleWindow) -> ClusterRead:
        """Read the cluster through command invoke."""
        return ClusterRead(self.readers.invoke(self.record).read(window), self.record.pools, (), ())


class AksSource:
    """The AKS clusters of each enabled subscription of the signed-in directory."""

    def __init__(self, cli: AzureCli, scope: AzureScope, readers: Readers, no_invoke: bool) -> None:
        """List scope's clusters through cli; with no_invoke, name the clusters only command invoke could read."""
        self._cli = cli
        self._scope = scope
        self._readers = readers
        self._no_invoke = no_invoke

    def clusters(self, progress: Progress) -> Found:
        """List each subscription's clusters, a few subscriptions at a time."""
        subscriptions = self._scope.subscriptions
        results = parallel(self._cli.clusters, [sub.id for sub in subscriptions], progress)
        targets: List[Target] = []
        named: List[NotAssessed] = []
        unread: List[Unread] = []
        for sub, result in zip(subscriptions, results):
            if isinstance(result, AssessmentError):
                unread.append(Unread(AZURE, sub.name, str(result)))
                continue
            for record in sorted(result, key=lambda each: each.name):
                way = self._way(record)
                if isinstance(way, str):
                    named.append(NotAssessed(AZURE, record.subscription, record.location, record.name, way))
                else:
                    targets.append(way)
        empty = sum(1 for result in results if not isinstance(result, AssessmentError) and not result)
        names = {sub.id: sub.name for sub in subscriptions}
        return Found(tuple(targets), tuple(named), tuple(unread), empty, names)

    def _way(self, record: AksCluster) -> Union[Target, str]:
        """Give the read method a cluster's settings choose, or why it is not read."""
        if record.power == STOPPED:
            return STOPPED_REASON
        access = record.access
        if access is None or not (access.private_cluster or access.authorized_ip_ranges):
            return AksDirect(self._cli, record, self._readers)
        if access.run_command_disabled:
            return INVOKE_OFF
        if self._no_invoke:
            return NO_INVOKE
        return AksInvoke(record, self._readers)


@dataclass(frozen=True)
class EksDirect:
    """An EKS cluster read with kubectl."""

    cli: AwsCli
    account: str
    record: EksCluster
    node_groups: Tuple[NodeGroup, ...]
    readers: Readers

    @property
    def cluster(self) -> ClusterId:
        """Name the cluster: its account, its region and its name."""
        return ClusterId(AWS, self.account, self.record.region, self.record.name)

    def read(self, window: SampleWindow) -> ClusterRead:
        """Write the cluster's kubeconfig, read it with kubectl, then the instances of nodes no label places."""
        region = self.record.region
        path = self.cli.write_kubeconfig(region, self.record.name)
        try:
            snapshot = self.readers.direct(Kubeconfig(path, False)).read(window)
        except ApiUnreachable:
            # Only a run inside its VPC reaches a private-only endpoint.
            if self.record.public_endpoint:
                raise
            raise ApiUnreachable(PRIVATE_ENDPOINT) from None
        if not any(unmarked(node) for node in snapshot.nodes):
            return ClusterRead(snapshot, (), self.node_groups, ())
        try:
            instances = self.cli.instances(region)
        except AssessmentError as error:
            raise type(error)(f"could not read its EC2 instances: {error}") from error
        return ClusterRead(snapshot, (), self.node_groups, instances)


class EksSource:
    """The EKS clusters of each enabled region of the signed-in account."""

    def __init__(self, cli: AwsCli, scope: AwsScope, readers: Readers) -> None:
        """List scope's clusters through cli."""
        self._cli = cli
        self._scope = scope
        self._readers = readers

    def clusters(self, progress: Progress) -> Found:
        """List each region's clusters, a few regions at a time."""
        regions = self._scope.regions
        results = parallel(self._region, regions, progress)
        targets: List[Target] = []
        named: List[NotAssessed] = []
        unread: List[Unread] = []
        for region, result in zip(regions, results):
            if isinstance(result, AssessmentError):
                unread.append(Unread(AWS, region, str(result)))
                continue
            for way in result:
                if isinstance(way, NotAssessed):
                    named.append(way)
                else:
                    targets.append(way)
        empty = sum(1 for result in results if not isinstance(result, AssessmentError) and not result)
        return Found(tuple(targets), tuple(named), tuple(unread), empty, {})

    def _region(self, region: str) -> List[Union[Target, NotAssessed]]:
        """Read a region's clusters, each with the read method its status allows, or why it is not read."""
        return [self._cluster(region, name) for name in self._cli.cluster_names(region)]

    def _cluster(self, region: str, name: str) -> Union[Target, NotAssessed]:
        """Read one cluster's record and node groups."""
        try:
            record = self._cli.cluster(region, name)
        except AssessmentError as error:
            return NotAssessed(AWS, self._scope.account, region, name, f"could not read the cluster: {error}")
        if record.status != ACTIVE:
            return NotAssessed(AWS, self._scope.account, region, name, f"the cluster's status is {record.status}")
        try:
            groups = self._cli.node_groups(region, name)
        except AssessmentError as error:
            return NotAssessed(AWS, self._scope.account, region, name, f"could not read its node groups: {error}")
        return EksDirect(self._cli, self._scope.account, record, tuple(groups), self._readers)
