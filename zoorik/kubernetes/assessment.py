"""The Kubernetes part of the run: every AKS and EKS cluster read and priced, and what the steps save on each.

Each cluster is read the way its settings choose, a few at a time. The machine types of each region with a pool whose
type may change are read once, and each such pool's candidates are planned. Machines and candidates are priced at list
prices, at spot for a pool on spot today, and at spot too for the quiet hours of step 6; the steps run on each cluster
in order. A cluster that can't be read, a line that can't be priced, and a cluster whose cheaper machine types or
fewer copies can't be counted are named with the reason.
"""

from collections import Counter
from dataclasses import dataclass, replace
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from zoorik.cloud.aws import AwsCli, AwsScope
from zoorik.cloud.azure import AzureCli, AzureScope
from zoorik.cloud.keys import AWS, AZURE, PLATFORMS
from zoorik.console import ConsolePart
from zoorik.errors import AssessmentError, PriceNotFound
from zoorik.format import count
from zoorik.kubectl.usage import SampleWindow
from zoorik.kubernetes.allocatable import ClusterNodes
from zoorik.kubernetes.catalogue import AwsCatalogue, AzureCatalogue, MachineCatalogue, MachineType
from zoorik.kubernetes.collect import NO_CONTROLLER, Collected, collect
from zoorik.kubernetes.discovery import AksSource, ClusterSource, EksSource, Found, Readers, Target
from zoorik.kubernetes.model import (
    AppResult,
    ClusterId,
    ClusterRead,
    ClusterResult,
    ClusterState,
    KubernetesResult,
    Line,
    LineResult,
    NotAssessed,
)
from zoorik.kubernetes.shares import workload_results
from zoorik.kubernetes.steps import SHARED_ROW, Pipeline, line_cost
from zoorik.kubernetes.steps.cheaper_types import CheaperTypes, Offer, Plan, Search, plans, searchable
from zoorik.kubernetes.steps.fewer_copies import FewerCopies
from zoorik.kubernetes.steps.pack_across_pools import PackAcrossPools
from zoorik.kubernetes.steps.smaller_pods import SmallerPods
from zoorik.kubernetes.steps.spare_machines import SpareMachines
from zoorik.kubernetes.steps.spot import Spot
from zoorik.pricing.machines import Capacity, MachinePrices
from zoorik.system.workers import parallel

# How each cloud's lines name the scope its clusters are listed in.
SCOPE_WORDS = {AZURE: "Subscription", AWS: "Region"}

# What a line is priced by: its cloud, region, machine type and capacity.
PriceKey = Tuple[str, str, str, Capacity]
# A line's hourly price, or why it has none.
Price = Union[float, str]
# A region's machine types are read once for each subscription or account: its cloud, account and region.
RegionKey = Tuple[str, str, str]
# A cluster's planned pools by pool id, or why its cheaper machine types can't be counted.
Planned = Union[Dict[str, Plan], str]


@dataclass(frozen=True)
class ClusterCloud:
    """One cloud as the Kubernetes part reads it: its clusters, its machine types and their list prices."""

    key: str
    source: ClusterSource
    prices: MachinePrices
    catalogue: MachineCatalogue


def azure_clusters(
    cli: AzureCli, scope: AzureScope, prices: MachinePrices, readers: Readers, no_invoke: bool
) -> ClusterCloud:
    """Read the AKS clusters of scope's subscriptions through cli and readers, priced with prices."""
    return ClusterCloud(AZURE, AksSource(cli, scope, readers, no_invoke), prices, AzureCatalogue(cli))


def aws_clusters(cli: AwsCli, scope: AwsScope, prices: MachinePrices, readers: Readers) -> ClusterCloud:
    """Read the EKS clusters of scope's regions through cli and readers, priced with prices."""
    return ClusterCloud(AWS, EksSource(cli, scope, readers), prices, AwsCatalogue(cli))


@dataclass(frozen=True)
class Reading:
    """A cluster the run read: its name, what was read, and the cluster as the steps see it."""

    cluster: ClusterId
    source: ClusterRead
    collected: Collected


class KubernetesAssessment:
    """The Kubernetes part: reads each cloud's clusters, prices their machines, and runs the steps on each."""

    def __init__(self, clouds: Sequence[ClusterCloud], window: SampleWindow, console: ConsolePart) -> None:
        """Assess the clusters of clouds, in their order, sampling usage over window and writing to console."""
        self._clouds = {cloud.key: cloud for cloud in clouds}
        self._window = window
        self._console = console

    def run(self) -> KubernetesResult:
        """Assess every cluster."""
        found = [self._discover(cloud) for cloud in self._clouds.values()]
        targets = [target for each in found for target in each.targets]
        results = parallel(self._read, targets, self._console.progress("clusters"))
        assessed: List[Reading] = []
        failed: List[NotAssessed] = []
        for target, result in zip(targets, results):
            cluster = target.cluster
            if isinstance(result, AssessmentError):
                failed.append(NotAssessed(cluster.cloud, cluster.account, cluster.region, cluster.name, str(result)))
                self._console.line(f"Cluster {cluster.name} ({cluster.region}): not assessed, {result}")
            else:
                assessed.append(Reading(cluster, result, collect(cluster.cloud, result)))
                self._console.line(read_line(cluster, result))
        planned = self._plans(assessed)
        prices = self._prices(assessed, planned)
        return KubernetesResult(
            clusters=tuple(assess(each, prices, planned.get(each.cluster, {})) for each in assessed),
            not_assessed=(*(named for each in found for named in each.not_assessed), *failed),
            unread=tuple(scope for each in found for scope in each.unread),
            subscriptions={key: name for each in found for key, name in each.names.items()},
        )

    def _discover(self, cloud: ClusterCloud) -> Found:
        """List one cloud's clusters, and say which scopes and clusters are left out, and why."""
        word = SCOPE_WORDS[cloud.key]
        found = cloud.source.clusters(self._console.progress(f"{word.lower()}s"))
        for scope in found.unread:
            self._console.line(f"{word} {scope.name}: clusters not listed, {scope.reason}")
        if found.empty:
            self._console.line(f"{count(found.empty, word.lower())} with no {PLATFORMS[cloud.key]} clusters.")
        for named in found.not_assessed:
            self._console.line(f"Cluster {named.name} ({named.region}): not assessed, {named.reason}")
        return found

    def _read(self, target: Target) -> ClusterRead:
        """Read one cluster, its usage sampled over the window."""
        return target.read(self._window)

    def _plans(self, assessed: Sequence[Reading]) -> Dict[ClusterId, Planned]:
        """Plan the candidates of each cluster's pools whose type may change, reading each region's types once.

        A list of machine types that can't be read is named once for each cloud.
        """
        wanted = {each.cluster: searchable(each.collected.state) for each in assessed}
        regions = sorted({region_key(cluster) for cluster, pools in wanted.items() if pools})
        listed = dict(zip(regions, parallel(self._types, regions, self._console.progress("machine type lists"))))
        planned: Dict[ClusterId, Planned] = {}
        unreadable: Dict[str, List[str]] = {}
        for each in assessed:
            cluster, pools = each.cluster, wanted[each.cluster]
            if not pools:
                continue
            types = listed[region_key(cluster)]
            if isinstance(types, AssessmentError):
                planned[cluster] = f"machine types not readable for {cluster.region}"
                unreadable.setdefault(cluster.cloud, []).append(str(types))
            else:
                planned[cluster] = plans(cluster_nodes(each), pools, types)
        for cloud in self._clouds:
            if cloud in unreadable:
                self._console.line(f"Machine types: {min(unreadable[cloud])}")
        return planned

    def _types(self, key: RegionKey) -> Mapping[str, MachineType]:
        """Read the machine types one region offers a subscription or account."""
        cloud, account, region = key
        return self._clouds[cloud].catalogue.types(account, region)

    def _prices(self, assessed: Sequence[Reading], planned: Mapping[ClusterId, Planned]) -> Dict[PriceKey, Price]:
        """Price each machine type, and each candidate, once per region and capacity, a few at a time.

        A type bought at the regular price is priced at spot too, for step 6. Each region's price list is read first,
        in as few calls as it allows. A price list that can't be read is named once for each cloud.
        """
        found: Set[PriceKey] = set()
        for each in assessed:
            found |= price_keys(each) | candidate_keys(each.cluster, planned.get(each.cluster, {}))
        found |= {(key[0], key[1], key[2], Capacity.SPOT) for key in found if key[3] is Capacity.REGULAR}
        keys = sorted(found, key=lambda key: (key[0], key[1], key[2], key[3].value))
        regions: Dict[Tuple[str, str], Set[str]] = {}
        for cloud, region, machine_type, _ in keys:
            regions.setdefault((cloud, region), set()).add(machine_type)
        parallel(self._read_prices, sorted(regions.items()), self._console.progress("price lists"))
        results = parallel(self._hourly, keys, self._console.progress("machine prices"))
        prices: Dict[PriceKey, Price] = {}
        unreadable: Dict[str, List[str]] = {}
        for key, result in zip(keys, results):
            if isinstance(result, PriceNotFound):
                prices[key] = str(result)
            elif isinstance(result, AssessmentError):
                prices[key] = f"price list not readable for {key[1]}"
                unreadable.setdefault(key[0], []).append(str(result))
            else:
                prices[key] = result
        for cloud in self._clouds:
            if cloud in unreadable:
                self._console.line(f"Machine list prices: {min(unreadable[cloud])}")
        return prices

    def _read_prices(self, item: Tuple[Tuple[str, str], Set[str]]) -> None:
        """Read one region's prices for its machine types; a failure is kept for hourly to raise."""
        (cloud, region), machine_types = item
        self._clouds[cloud].prices.read(region, machine_types)

    def _hourly(self, key: PriceKey) -> float:
        """Give one machine type's hourly list price in a region, bought one way."""
        cloud, region, machine_type, capacity = key
        return self._clouds[cloud].prices.hourly(region, machine_type, capacity)


def read_line(cluster: ClusterId, read: ClusterRead) -> str:
    """Say what was read of a cluster, and why its usage wasn't read, when it wasn't."""
    snapshot = read.snapshot
    nodes, pods = count(len(snapshot.nodes), "node"), count(len(snapshot.pods), "pod")
    reason = snapshot.usage.reason
    usage = "" if reason is None else f" (usage not read: {reason})"
    return f"Cluster {cluster.name} ({cluster.region}): {nodes}, {pods}{usage}"


def price_key(cluster: ClusterId, line: Line) -> Optional[PriceKey]:
    """Give what a line is priced by; None for a line that can't have a price."""
    if line.note or line.machine_type is None or line.capacity is None:
        return None
    return (cluster.cloud, cluster.region, line.machine_type, line.capacity)


def price_keys(reading: Reading) -> Set[PriceKey]:
    """Give what each of a cluster's lines that can have a price is priced by."""
    keys = (price_key(reading.cluster, line) for pool in reading.collected.state.pools for line in pool.lines)
    return {key for key in keys if key is not None}


def region_key(cluster: ClusterId) -> RegionKey:
    """Give the region whose machine types a cluster's pools may move to, as the subscription or account sees it."""
    return (cluster.cloud, cluster.account, cluster.region)


def candidate_keys(cluster: ClusterId, planned: Planned) -> Set[PriceKey]:
    """Give what each planned candidate is priced by: its type, bought as its pool's machines are."""
    if isinstance(planned, str):
        return set()
    return {
        (cluster.cloud, cluster.region, each.kind.name, plan.capacity)
        for plan in planned.values()
        for each in plan.candidates
    }


def cluster_nodes(reading: Reading) -> ClusterNodes:
    """Give a cluster as the allocatable rules are checked on it: its nodes and agent pools by name."""
    return ClusterNodes(
        cloud=reading.cluster.cloud,
        state=reading.collected.state,
        nodes={node.name: node for node in reading.source.snapshot.nodes},
        agent_pools={pool.name: pool for pool in reading.source.agent_pools},
    )


def searches(cluster: ClusterId, planned: Mapping[str, Plan], prices: Mapping[PriceKey, Price]) -> Dict[str, Search]:
    """Give each planned pool's search: its candidates that have a price, at the price its machines are bought at."""
    found: Dict[str, Search] = {}
    for pool, plan in planned.items():
        offers = []
        for each in plan.candidates:
            price = prices[(cluster.cloud, cluster.region, each.kind.name, plan.capacity)]
            # A candidate the price list has no price for can't be compared, so it is not offered.
            if not isinstance(price, str):
                kind, room = each.kind, each.room
                offers.append(Offer(kind.name, kind.architecture, price, room.allocatable, room.attach))
        found[pool] = Search(tuple(offers), plan.zones, plan.capacity, plan.one_type)
    return found


def spot_prices(cluster: ClusterId, prices: Mapping[PriceKey, Price]) -> Dict[str, Price]:
    """Give each machine type's spot price in a cluster's region, or why it has none."""
    return {
        machine_type: price
        for (cloud, region, machine_type, capacity), price in prices.items()
        if (cloud, region, capacity) == (cluster.cloud, cluster.region, Capacity.SPOT)
    }


def priced(cluster: ClusterId, state: ClusterState, prices: Mapping[PriceKey, Price]) -> ClusterState:
    """Give the state with each line's hourly price, or why it has none."""

    def line_priced(line: Line) -> Line:
        key = price_key(cluster, line)
        if key is None:
            return line
        price = prices[key]
        return replace(line, note=price) if isinstance(price, str) else replace(line, hourly=price)

    pools = tuple(replace(pool, lines=tuple(line_priced(line) for line in pool.lines)) for pool in state.pools)
    return replace(state, pools=pools)


def line_results(today: ClusterState, after: ClusterState) -> Tuple[LineResult, ...]:
    """Give each line's machines and cost a month, today and after the steps; an unpriced line costs nothing in both.

    The lines are after's: today's, and those of the machine types and pools the steps add, with no machines today. A
    line a step adds that a later step empties has no machines today or after, so it is left out; its machines at quiet
    times are among those after.
    """
    before, left, quiet = today.counts(), after.counts(), after.quiet_counts()
    results: List[LineResult] = []
    for pool in after.pools:
        for line in pool.lines:
            key = (pool.id, line.machine_type, line.capacity)
            machines_today, machines_after, machines_quiet = before.get(key, 0), left.get(key, 0), quiet.get(key, 0)
            if machines_today == machines_after == 0:
                continue
            hourly = line.hourly
            results.append(
                LineResult(
                    pool=pool.name,
                    kind=pool.kind,
                    machine_type=line.machine_type,
                    capacity=None if line.capacity is None else line.capacity.value,
                    hourly=hourly,
                    note=line.note,
                    today=machines_today,
                    after=machines_after,
                    quiet=machines_quiet,
                    spot=line.spot,
                    spot_note=line.spot_note,
                    cost_today=0 if hourly is None else line_cost(machines_today, machines_today, hourly, None),
                    cost_after=0 if hourly is None else line_cost(machines_after, machines_quiet, hourly, line.spot),
                )
            )
    return tuple(results)


def app_results(after: ClusterState) -> Tuple[AppResult, ...]:
    """Give each app that runs fewer copies at quiet times, by namespace and name; none when no app does."""
    if after.quiet is None:
        return ()
    copies = Counter(pod.workload for pod in after.pods)
    quiet = Counter(pod.workload for pod in after.quiet.pods)
    apps = [
        AppResult(app.kind, app.namespace, app.name, number, quiet[app])
        for app, number in copies.items()
        if quiet[app] < number
    ]
    return tuple(sorted(apps, key=lambda app: (app.namespace, app.name, app.kind)))


def assess(reading: Reading, prices: Mapping[PriceKey, Price], planned: Planned) -> ClusterResult:
    """Price one cluster's machines and run the steps on it, step 3 on its planned pools."""
    snapshot = reading.source.snapshot
    today = priced(reading.cluster, reading.collected.state, prices)
    cheaper = CheaperTypes({} if isinstance(planned, str) else searches(reading.cluster, planned, prices))
    pipeline = Pipeline(
        [
            SmallerPods(snapshot.usage.peaks, snapshot.limit_ranges),
            SpareMachines(),
            cheaper,
            PackAcrossPools(),
            FewerCopies(snapshot.scaling),
            Spot(spot_prices(reading.cluster, prices)),
        ]
    )
    after, steps = pipeline.run(today)
    shared = sum(step.saving for step in steps if step.row == SHARED_ROW)
    return ClusterResult(
        cluster=reading.cluster,
        lines=line_results(today, after),
        steps=steps,
        workloads=workload_results(today, after, shared),
        apps=app_results(after),
        bare_pods=sum(1 for pod in today.pods if pod.stays == NO_CONTROLLER),
        not_machines=reading.collected.not_machines,
        usage=snapshot.usage.reason,
        cheaper_types=planned if isinstance(planned, str) else None,
        fewer_copies=snapshot.scaling.reason,
    )
