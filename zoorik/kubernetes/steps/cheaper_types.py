"""Step 3, cheaper machine types: each pool moves to the cheapest type, or mix of two types, that the search finds.

A candidate type has the pool's CPU architecture, doesn't burst, has no GPU, is offered to the subscription or account
in every zone the pool uses, takes premium disks where the pool's pods use them and Ultra Disk where they use that,
and on AKS has at least 2 vCPUs and 4 GiB. A pool keeps its types when it runs GPUs, when one of its lines has no
price or its machines are bought two ways, and when a pod on it selects its machine type or requests a resource that
comes with the type, such as a GPU or hugepages.

The search: each candidate gets a lower bound, its price times the machines the pool's pods need on their scarcest
resource. Single types are packed in lower-bound order until the next bound reaches the cheapest cost found; a type is
skipped when one tried before it, for the same pool, covers it: it gives at least as much of every resource and attach
slot at no higher price, so types that give the same room at the same price count once. Then, from the cheapest single
type, for each k the pods of its k least-full machines (ties: the last opened) move to new machines of a type it
doesn't cover, packed in the same order and with the same stop, while its other machines keep their pods; k stops one
short of all its machines, which would be a single type again. A pool's search runs at most 20 packings, the mixes
after the single types. The pool takes the cheapest of these when it costs less than the pool does.

An AKS node pool or an EKS managed node group runs one type, so a mix's second type becomes a new pool of the same
kind: it takes the pool's labels, but not its name labels, and its taints, and runs in User mode, so a pod that selects
the pool's name stays on the first type. A Karpenter NodePool or an Auto Scaling group holds both types itself.
"""

from collections import Counter
from dataclasses import dataclass, replace
from typing import AbstractSet, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple, Union

from zoorik.cloud.keys import AZURE
from zoorik.kubectl.objects import HostPort, PersistentVolume, Taint
from zoorik.kubectl.objects import Pod as ApiPod
from zoorik.kubectl.quantity import CPU
from zoorik.kubernetes.allocatable import EPHEMERAL_STORAGE, ClusterNodes, TypeRoom, machine_model
from zoorik.kubernetes.catalogue import AZURE_DISK, MachineType
from zoorik.kubernetes.collect import AGENT_POOL, AGENT_POOL_LABEL, NODE_GROUP, NODE_GROUP_LABEL, TYPE_LABEL
from zoorik.kubernetes.model import MEMORY, PODS, ClusterState, Line, Machine, Pod, Pool, Resources
from zoorik.kubernetes.packing import Job, largest_share, pack
from zoorik.kubernetes.placement import (
    ARCH_LABEL,
    HOSTNAME,
    Overhead,
    Placed,
    added,
    demand_of,
    empty_room,
    host_ports,
    node_selected,
    overheads,
    same_platform,
    tolerated,
    type_label,
    volumes_reach,
)
from zoorik.kubernetes.steps import line_cost, minimum
from zoorik.pricing.machines import Capacity

ROW = "cheaper machine types"
NEW = "new"
# The resources a pod may request on a pool whose type may change; any other comes with the type.
PLAIN_RESOURCES = frozenset({CPU, MEMORY, PODS, EPHEMERAL_STORAGE})
# The pool kinds that run one machine type, so that a second type goes to a new pool of the same kind.
ONE_TYPE_KINDS = (AGENT_POOL, NODE_GROUP)
# The labels that name such a pool, which a pool added beside it doesn't carry.
NAME_LABELS = ("agentpool", AGENT_POOL_LABEL, NODE_GROUP_LABEL)
MODE_LABEL = "kubernetes.azure.com/mode"
USER_MODE = "user"
BETA_TYPE_LABEL = "beta.kubernetes.io/instance-type"
# The Azure disk tiers that need a size with PremiumIO, as a volume names them in lowercase; Ultra Disk needs more.
PREMIUM_TIERS = frozenset({"premium_lrs", "premium_zrs", "premiumv2_lrs", "ultrassd_lrs"})
ULTRA_TIER = "ultrassd_lrs"
# AKS refuses a node size below 2 vCPUs and 4 GiB.
AKS_MIN_VCPUS = 2
AKS_MIN_MEMORY_MIB = 4096
# The most packings one pool's search runs, which keeps step 3 on a pool of 3,000 pods and 300 candidate types within a
# minute; the single types come first, then the mixes, k by k.
PACKINGS = 20

# Machines counted by pool id and machine type, which is what a way to hold a pool costs by.
Counts = Dict[Tuple[str, Optional[str]], int]


# ---------------------------------------------------------------- which pools may change, and to what


@dataclass(frozen=True)
class Searchable:
    """A pool whose machine type may change, as far as the cluster alone tells, and how its machines are bought."""

    pool: Pool
    capacity: Capacity


@dataclass(frozen=True)
class Candidate:
    """A machine type a pool may move to, and what a new machine of it gives the pool's pods."""

    kind: MachineType
    room: TypeRoom


@dataclass(frozen=True)
class Zone:
    """A zone a pool runs in, and the labels and taints a new machine there gets, besides its name and type."""

    name: str
    labels: Mapping[str, str]
    taints: Tuple[Taint, ...]


@dataclass(frozen=True)
class Plan:
    """A pool's candidates before their prices: where its new machines go, how they're bought, and its kind's rule."""

    candidates: Tuple[Candidate, ...]
    zones: Tuple[Zone, ...]
    capacity: Capacity
    # The pool's kind runs one machine type, so a second type goes to a new pool.
    one_type: bool


@dataclass(frozen=True)
class Needs:
    """What a pool's new machines must have: an architecture, its zones, premium disks, Ultra Disk, the AKS minimum."""

    architectures: FrozenSet[str]
    zones: FrozenSet[str]
    premium: bool
    ultra: bool
    aks: bool


def pool_machines(state: ClusterState, pool: Pool) -> List[Machine]:
    """Give a pool's machines."""
    return [machine for machine in state.machines if machine.pool == pool.id]


def pool_pods(state: ClusterState, pool: Pool) -> List[Pod]:
    """Give the pods on a pool's machines, its DaemonSet and static pods included."""
    names = {machine.name for machine in pool_machines(state, pool)}
    return [pod for pod in state.pods if pod.node in names]


def selects_type(pod: ApiPod) -> bool:
    """Tell whether a pod's node selector or required node affinity names a label that comes from the machine type."""
    keys = set(pod.node_selector)
    if pod.node_affinity is not None:
        keys |= {item.key for term in pod.node_affinity for item in term.match_expressions}
    return any(type_label(key) for key in keys)


def plain(pod: Pod) -> bool:
    """Tell whether a pod requests nothing beyond CPU, memory, a pod slot and ephemeral storage."""
    return all(resource in PLAIN_RESOURCES for resource, amount in pod.request.items() if amount > 0)


def bought(pool: Pool) -> Optional[Capacity]:
    """Give the one way a pool's machines are bought; None when a line can't be priced or they're bought two ways."""
    capacities = {None if line.note or line.machine_type is None else line.capacity for line in pool.lines}
    return capacities.pop() if len(capacities) == 1 else None


def searchable(state: ClusterState) -> Tuple[Searchable, ...]:
    """Give the pools whose machine type may change, as far as the cluster alone tells.

    Each of a pool's lines must be one the price list may price, all bought one way, and no pod on its machines may
    select its machine type or request a resource beyond CPU, memory, a pod slot and ephemeral storage.
    """
    found = []
    for pool in state.pools:
        capacity = bought(pool)
        if capacity is not None and all(plain(pod) and not selects_type(pod.source) for pod in pool_pods(state, pool)):
            found.append(Searchable(pool, capacity))
    return tuple(found)


def premium(volume: PersistentVolume) -> bool:
    """Tell whether a volume is an Azure disk of a premium tier; an in-tree azureDisk, which names no tier, counts."""
    if volume.driver != AZURE_DISK:
        return False
    return volume.in_tree or (volume.tier is not None and volume.tier.lower() in PREMIUM_TIERS)


def ultra(volume: PersistentVolume) -> bool:
    """Tell whether a volume is an Ultra Disk."""
    return volume.driver == AZURE_DISK and volume.tier is not None and volume.tier.lower() == ULTRA_TIER


def needs_of(cluster: ClusterNodes, pool: Pool) -> Needs:
    """Give what a pool's new machines must have for its pods."""
    machines = pool_machines(cluster.state, pool)
    volumes = [volume for pod in pool_pods(cluster.state, pool) for volume in pod.volumes]
    return Needs(
        architectures=frozenset(machine.labels[ARCH_LABEL] for machine in machines if ARCH_LABEL in machine.labels),
        zones=frozenset(machine.zone for machine in machines),
        premium=any(premium(volume) for volume in volumes),
        ultra=any(ultra(volume) for volume in volumes),
        aks=cluster.cloud == AZURE,
    )


def offered(kind: MachineType, needs: Needs, zonal: FrozenSet[str]) -> bool:
    """Tell whether a type is offered, with Ultra Disk where it's needed, in each of the pool's zones.

    A zone the region's types don't list, such as that of a machine in no zone, needs the type offered in the region.
    """
    for zone in needs.zones:
        if not (zone in kind.zones if zone in zonal else kind.regional):
            return False
        if needs.ultra and not (zone in kind.ultra_zones if zone in zonal else kind.ultra_regional):
            return False
    return True


def candidate(kind: MachineType, needs: Needs, zonal: FrozenSet[str]) -> bool:
    """Tell whether a type passes the candidate filters for a pool."""
    if kind.architecture not in needs.architectures or kind.burstable or kind.gpus:
        return False
    if needs.aks and (kind.vcpus < AKS_MIN_VCPUS or kind.memory_mib < AKS_MIN_MEMORY_MIB):
        return False
    if needs.premium and not kind.premium_io:
        return False
    return offered(kind, needs, zonal)


def zones_of(state: ClusterState, pool: Pool) -> Tuple[Zone, ...]:
    """Give the pool's zones, each with the labels its machines there share and the taints all its machines share.

    A machine's own name and its type's labels are left out: a new machine gets its own.
    """
    machines = pool_machines(state, pool)
    taints = tuple(taint for taint in machines[0].taints if all(taint in machine.taints for machine in machines))
    zones = []
    for zone in sorted({machine.zone for machine in machines}):
        here = [machine.labels for machine in machines if machine.zone == zone]
        shared = {
            key: value
            for key, value in here[0].items()
            if key != HOSTNAME and not type_label(key) and all(labels.get(key) == value for labels in here)
        }
        zones.append(Zone(zone, shared, taints))
    return tuple(zones)


def runs_gpus(pool: Pool, types: Mapping[str, MachineType]) -> bool:
    """Tell whether one of the pool's machine types has GPUs; a type the region's list lacks is the model's to name."""
    for line in pool.lines:
        kind = types.get("" if line.machine_type is None else line.machine_type.lower())
        if kind is not None and kind.gpus:
            return True
    return False


def plans(
    cluster: ClusterNodes, pools: Sequence[Searchable], types: Mapping[str, MachineType]
) -> Union[Dict[str, Plan], str]:
    """Give each pool's candidates, or why the cluster's nodes leave the model untrusted; GPU pools keep their type."""
    kept = [each for each in pools if not runs_gpus(each.pool, types)]
    if not kept:
        return {}
    model = machine_model(cluster, [each.pool for each in kept], types)
    if isinstance(model, str):
        return model
    zonal = frozenset(zone for kind in types.values() for zone in kind.zones)
    found: Dict[str, Plan] = {}
    for each in kept:
        pool = each.pool
        needs = needs_of(cluster, pool)
        rooms = [
            (kind, model.room(pool.id, kind))
            for kind in sorted(types.values(), key=lambda kind: kind.name)
            if candidate(kind, needs, zonal)
        ]
        candidates = tuple(Candidate(kind, room) for kind, room in rooms if room is not None)
        found[pool.id] = Plan(candidates, zones_of(cluster.state, pool), each.capacity, pool.kind in ONE_TYPE_KINDS)
    return found


# ---------------------------------------------------------------- the search


@dataclass(frozen=True)
class Offer:
    """A candidate with its hourly price: a machine type step 3 may move a pool to, and what it gives pods."""

    machine_type: str
    architecture: str
    hourly: float
    allocatable: Resources
    attach: Mapping[str, int]


@dataclass(frozen=True)
class Search:
    """What step 3 may move one pool to: its offers, the zones new machines go in, and how they're bought."""

    offers: Tuple[Offer, ...]
    zones: Tuple[Zone, ...]
    capacity: Capacity
    one_type: bool


@dataclass(frozen=True)
class Outcome:
    """A way to hold a pool's pods: its cost a month, the pool's machines (a new pool's included) and each pod's."""

    cost: int
    machines: Tuple[Machine, ...]
    where: Mapping[str, str]


@dataclass(frozen=True)
class Split:
    """Pods to pack onto new machines of one type, their demand, and the machines that stay as they are.

    The pods may also go on the opened machines, whose spare room the lower bound counts. The closed machines keep the
    pods on them and take no more.
    """

    pods: Tuple[Pod, ...]
    demand: Resources
    opened: Tuple[Machine, ...]
    spare: Resources
    closed: Tuple[Machine, ...]
    # The pool's moving pods by the machine they are on; those on a closed machine stay there.
    on: Mapping[str, Sequence[Pod]]


class CheaperTypes:
    """Moves each pool with offers to the cheapest type, or mix of two, the search finds, when that costs less."""

    name = "Cheaper machine types"
    row = ROW
    assumption = ""

    def __init__(self, searches: Mapping[str, Search]) -> None:
        """Search each pool searches names, by pool id."""
        self._searches = searches

    def apply(self, state: ClusterState) -> ClusterState:
        """Search each priced pool with offers in turn, by pool id."""
        for pool in sorted(state.pools, key=lambda each: each.id):
            search = self._searches.get(pool.id)
            if search is not None and search.offers and pool.priced:
                outcome = PoolSearch(state, pool, search).best()
                if outcome is not None:
                    state = applied(state, pool, search, outcome)
        return state


def covers(offer: Offer, other: Offer) -> bool:
    """Tell whether an offer gives at least what another does at no higher price.

    That is the same architecture, at least as much of each resource the other gives, and no CSI driver limited to
    fewer attach slots than the other allows.
    """
    return (
        offer.architecture == other.architecture
        and offer.hourly <= other.hourly
        and all(offer.allocatable.get(key, 0) >= amount for key, amount in other.allocatable.items())
        and all(driver in other.attach and other.attach[driver] <= limit for driver, limit in offer.attach.items())
    )


def room_of(offer: Offer, overhead: Overhead) -> Optional[Dict[str, int]]:
    """Give what an empty machine of the offer has left for pods after the pool's overhead; None when that won't fit."""
    if any(amount > offer.allocatable.get(key, 0) for key, amount in overhead.requests.items()):
        return None
    return {key: amount - overhead.requests.get(key, 0) for key, amount in offer.allocatable.items()}


def machines_needed(demand: Resources, spare: Resources, room: Resources) -> Optional[int]:
    """Give the fewest machines of room that hold demand beyond spare, on the scarcest resource; None when none do."""
    needed = 0
    for resource, amount in demand.items():
        left = amount - spare.get(resource, 0)
        if left <= 0:
            continue
        each = room.get(resource, 0)
        if each <= 0:
            # No number of machines without room for a resource holds a demand for it.
            return None
        needed = max(needed, -(-left // each))
    return needed


def counts_of(machines: Sequence[Machine]) -> Counts:
    """Count machines by pool id and machine type."""
    return dict(Counter((machine.pool, machine.machine_type) for machine in machines))


def fits_new(pod: Pod, offer: Offer, room: Resources) -> bool:
    """Tell whether a pod's request and volumes fit an empty machine of the offer."""
    return all(amount <= room.get(resource, 0) for resource, amount in pod.request.items()) and all(
        sum(1 for volume in pod.volumes if volume.driver == driver) <= limit for driver, limit in offer.attach.items()
    )


def allows(pod: Pod, labels: Mapping[str, str], taints: Sequence[Taint], ports: FrozenSet[HostPort]) -> bool:
    """Tell whether a new machine with labels and taints passes a pod's rules that no other pod changes."""
    # A new machine has no name yet, so a rule that names a node never matches it.
    name = f"{NEW}/{pod.key}"
    return (
        node_selected(pod.source, labels, name)
        and tolerated(pod.source.tolerations, taints)
        and volumes_reach(pod.volumes, labels, name)
        and not host_ports(pod.source) & ports
        and same_platform(pod, labels)
    )


class PoolSearch:
    """One pool's search: single types in lower-bound order, then mixes of two from the cheapest single type."""

    def __init__(self, state: ClusterState, pool: Pool, search: Search) -> None:
        """Search pool, as state has it, among search's offers."""
        self._pool = pool
        self._search = search
        self._new_pool = f"{NEW} {pool.id}"
        overhead = overheads(state)
        self._overheads: Dict[str, Overhead] = {**overhead, self._new_pool: overhead[pool.id]}
        machines = pool_machines(state, pool)
        names = {machine.name for machine in machines}
        by_name = {machine.name: machine for machine in state.machines}
        pods = [pod for pod in state.pods if pod.node in names and not pod.overhead]
        pinned = {pod.node for pod in pods if pod.stays is not None}
        self._pinned = tuple(machine for machine in machines if machine.name in pinned)
        self._today = tuple(machines)
        self._moving = tuple(pod for pod in pods if pod.stays is None)
        self._fixed = tuple(
            Placed(pod, by_name[pod.node])
            for pod in state.pods
            if not pod.overhead and pod.node in by_name and (pod.node not in names or pod.stays is not None)
        )
        self._others = tuple(machine for machine in state.machines if machine.pool != pool.id)
        self._fixed_on: Dict[str, Dict[str, int]] = {}
        for placed in self._fixed:
            self._fixed_on[placed.machine.name] = added(self._fixed_on.get(placed.machine.name, {}), placed.pod.request)
        self._rooms: Dict[str, Dict[str, int]] = {}
        offers = []
        for offer in search.offers:
            room = room_of(offer, overhead[pool.id])
            if room is not None:
                self._rooms[offer.machine_type] = room
                offers.append(offer)
        self._offers = tuple(offers)
        self._prices: Dict[Optional[str], float] = {
            line.machine_type: line.hourly for line in pool.lines if line.hourly is not None
        }
        self._prices.update({offer.machine_type: offer.hourly for offer in search.offers})
        self._accepts: Dict[Tuple[str, str], Dict[str, FrozenSet[str]]] = {}
        self._misfits: Dict[Tuple[str, str], FrozenSet[str]] = {}
        self._packings = 0

    def best(self) -> Optional[Outcome]:
        """Give the cheapest way the search finds to hold the pool's pods, when it costs less than the pool does.

        The cheapest single type is found whatever it costs, since the mixes start from it.
        """
        current = self.cost(counts_of(self._today))
        single = Split(self._moving, demand_of(self._moving), self._pinned, self._leftover(self._pinned), (), {})
        cheapest = self._search_split(single, self._offers, None)
        if cheapest is None:
            return None
        # A mix is kept only when it costs less than the cheapest single type and the pool both.
        mix = self._best_mix(cheapest, min(current, cheapest.cost))
        found = cheapest if mix is None else mix
        return found if found.cost < current else None

    def cost(self, counts: Counts) -> int:
        """Give what machines counted by pool and type cost a month, each pool's type a line."""
        return sum(line_cost(number, number, self._prices[kind], None) for (_, kind), number in counts.items())

    # -------------------------------------------------------- both searches

    def _search_split(self, split: Split, offers: Sequence[Offer], bound: Optional[int]) -> Optional[Outcome]:
        """Pack the split's pods onto each offer in lower-bound order, until the next bound reaches the cheapest found.

        An offer is skipped when one tried before it, for the same pool, covers it. Give the cheapest packing, or None
        when no offer holds the pods below bound; None as bound sets no limit.
        """
        cheapest: Optional[Outcome] = None
        keys = {pod.key for pod in split.pods}
        tried: Dict[str, List[Offer]] = {}
        for lower, _, _, offer, target in sorted(self._ranked(split, offers), key=lambda item: item[:3]):
            # No offer after this one can cost less than the cheapest found, or the pool has run its packings.
            if (bound is not None and lower >= bound) or self._packings >= PACKINGS:
                break
            before = tried.setdefault(target, [])
            covered = any(covers(each, offer) for each in before)
            before.append(offer)
            # A covered offer holds no pod the one that covers it doesn't, for no less; a misfit holds not all of them.
            if covered or self._misfit(offer, target) & keys:
                continue
            outcome = self._pack(offer, target, split)
            if outcome is not None and (bound is None or outcome.cost < bound):
                cheapest, bound = outcome, outcome.cost
        return cheapest

    def _ranked(self, split: Split, offers: Sequence[Offer]) -> List[Tuple[int, float, str, Offer, str]]:
        """Give each offer that may hold the split's pods its lower bound, price, name and the pool it would join.

        The lower bound is what the staying machines cost, with as many new machines of the offer as the pods need
        beyond the opened machines' spare room on their scarcest resource, and at least the pool's minimum.
        """
        staying = (*split.opened, *split.closed)
        kept = counts_of(staying)
        types = {machine.machine_type for machine in staying}
        in_pool = sum(1 for machine in staying if machine.pool == self._pool.id)
        ranked = []
        for offer in offers:
            needed = machines_needed(split.demand, split.spare, self._rooms[offer.machine_type])
            if needed is not None:
                target = self._target(offer, types)
                if target == self._pool.id:
                    needed = max(needed, minimum(self._pool) - in_pool)
                key = (target, offer.machine_type)
                lower = self.cost({**kept, key: kept.get(key, 0) + needed})
                ranked.append((lower, offer.hourly, offer.machine_type, offer, target))
        return ranked

    def _pack(self, offer: Offer, target: str, split: Split) -> Optional[Outcome]:
        """Pack the split's pods onto its opened machines and new machines of the offer; None when they don't fit."""
        staying = tuple(Placed(pod, machine) for machine in split.closed for pod in split.on.get(machine.name, ()))
        job = Job(
            pods=split.pods,
            fixed=(*self._fixed, *staying),
            opened=split.opened,
            candidates=self._machines(offer, target, self._per_zone(split.pods, target, offer.architecture)),
            others=(*self._others, *split.closed),
            overheads=self._overheads,
            cap=len(split.opened) + len(split.pods),
        )
        self._packings += 1
        packing = pack(job)
        if packing is None:
            return None
        machines = self._with_minimum((*split.closed, *packing.machines), offer)
        where = {**{placed.pod.key: placed.machine.name for placed in staying}, **packing.where}
        return Outcome(self.cost(counts_of(machines)), machines, where)

    def _with_minimum(self, machines: Tuple[Machine, ...], offer: Offer) -> Tuple[Machine, ...]:
        """Give machines with the platform's minimum in the pool: any machine missing is one of the offer's."""
        missing = minimum(self._pool) - sum(1 for machine in machines if machine.pool == self._pool.id)
        if missing <= 0:
            return machines
        names = {machine.name for machine in machines}
        first = {self._search.zones[0].name: minimum(self._pool)}
        spare = [machine for machine in self._machines(offer, self._pool.id, first) if machine.name not in names]
        return (*machines, *spare[:missing])

    # -------------------------------------------------------- mixes of two types

    def _best_mix(self, base: Outcome, bound: int) -> Optional[Outcome]:
        """For each k, move the pods of base's k least-full machines to new machines of a type base's doesn't cover."""
        types = {machine.machine_type for machine in base.machines}
        if self._search.one_type and len(types) > 1:
            # A pool of one type that holds two already, its pinned machines' and a new pool's, takes no third.
            return None
        pinned = {machine.name for machine in self._pinned}
        movable = [machine for machine in base.machines if machine.name not in pinned]
        on: Dict[str, List[Pod]] = {}
        for pod in self._moving:
            on.setdefault(base.where[pod.key], []).append(pod)
        used = {name: demand_of(pods) for name, pods in on.items()}
        kinds = {machine.machine_type for machine in movable}
        held = [offer for offer in self._offers if offer.machine_type in kinds]
        # A type the moving pods' own type covers holds none of them better, its own type included.
        offers = [offer for offer in self._offers if not any(covers(each, offer) for each in held)]
        best: Optional[Outcome] = None
        emptied: Set[str] = set()
        pods: List[Pod] = []
        demand: Resources = {}
        for machine in self._least_full(movable, used)[:-1]:
            # The pool has run its packings, so no further k is packed.
            if self._packings >= PACKINGS:
                break
            emptied.add(machine.name)
            pods.extend(on.get(machine.name, ()))
            demand = added(demand, used.get(machine.name, {}))
            closed = tuple(each for each in base.machines if each.name not in emptied)
            outcome = self._search_split(Split(tuple(pods), demand, (), {}, closed, on), offers, bound)
            if outcome is not None:
                best, bound = outcome, outcome.cost
        return best

    def _least_full(self, movable: Sequence[Machine], used: Mapping[str, Resources]) -> List[Machine]:
        """Order machines least full first, by the largest share of their room the pods use; ties, the last opened."""

        def fullness(index: int) -> Tuple[object, ...]:
            machine = movable[index]
            room = empty_room(machine, self._overheads[machine.pool]).free
            return (largest_share(used.get(machine.name, {}), room), -index)

        return [movable[index] for index in sorted(range(len(movable)), key=fullness)]

    def _leftover(self, machines: Sequence[Machine]) -> Dict[str, int]:
        """Total the room machines have left after the pods that stay on them, never below 0 on one resource."""
        total: Dict[str, int] = {}
        for machine in machines:
            free = empty_room(machine, self._overheads[machine.pool]).free
            taken = self._fixed_on.get(machine.name, {})
            total = added(total, {key: max(0, amount - taken.get(key, 0)) for key, amount in free.items()})
        return total

    # -------------------------------------------------------- new machines

    def _target(self, offer: Offer, types: AbstractSet[Optional[str]]) -> str:
        """Give the pool new machines of the offer join: a new pool where the pool runs one type and keeps another."""
        return self._new_pool if self._search.one_type and types - {offer.machine_type} else self._pool.id

    def _misfit(self, offer: Offer, target: str) -> FrozenSet[str]:
        """Name the moving pods that fit no empty machine of the offer in target, in a zone they can run in."""
        key = (offer.machine_type, target)
        if key not in self._misfits:
            room = self._rooms[offer.machine_type]
            accepts = self._accepted(target, offer.architecture)
            self._misfits[key] = frozenset(
                pod.key for pod in self._moving if not accepts[pod.key] or not fits_new(pod, offer, room)
            )
        return self._misfits[key]

    def _accepted(self, target: str, architecture: str) -> Mapping[str, FrozenSet[str]]:
        """Give, for each moving pod, the zones where a new machine of target passes its rules that no type changes."""
        key = (target, architecture)
        if key not in self._accepts:
            ports = self._overheads[self._pool.id].ports
            zones = [(zone, self._labels(zone, target, architecture)) for zone in self._search.zones]
            self._accepts[key] = {
                pod.key: frozenset(zone.name for zone, labels in zones if allows(pod, labels, zone.taints, ports))
                for pod in self._moving
            }
        return self._accepts[key]

    def _labels(self, zone: Zone, target: str, architecture: str) -> Dict[str, str]:
        """Give the labels a new machine of target gets in zone: a new pool's lose the pool's name and run as User."""
        labels = {**zone.labels, ARCH_LABEL: architecture}
        if target == self._new_pool:
            for key in NAME_LABELS:
                labels.pop(key, None)
            if MODE_LABEL in labels:
                labels[MODE_LABEL] = USER_MODE
        return labels

    def _per_zone(self, pods: Sequence[Pod], target: str, architecture: str) -> Dict[str, int]:
        """Give how many new machines each zone may need: one for each pod that can run there."""
        accepts = self._accepted(target, architecture)
        return {zone.name: sum(1 for pod in pods if zone.name in accepts[pod.key]) for zone in self._search.zones}

    def _machines(self, offer: Offer, target: str, counts: Mapping[str, int]) -> Tuple[Machine, ...]:
        """Make new machines of the offer for target, as many in each zone as counts says, the zones in turn."""
        made = []
        beta = any(BETA_TYPE_LABEL in machine.labels for machine in self._today)
        for index in range(max(counts.values(), default=0)):
            for zone in self._search.zones:
                if index >= counts.get(zone.name, 0):
                    continue
                name = f"{target}/{offer.machine_type}/{zone.name}/{index}"
                labels = self._labels(zone, target, offer.architecture)
                labels.update({HOSTNAME: name, TYPE_LABEL: offer.machine_type})
                if beta:
                    labels[BETA_TYPE_LABEL] = offer.machine_type
                made.append(
                    Machine(
                        name=name,
                        pool=target,
                        machine_type=offer.machine_type,
                        capacity=self._search.capacity,
                        zone=zone.name,
                        labels=labels,
                        taints=zone.taints,
                        allocatable=offer.allocatable,
                        attach=offer.attach,
                    )
                )
        return tuple(made)


# ---------------------------------------------------------------- the state the step leaves


def applied(state: ClusterState, pool: Pool, search: Search, outcome: Outcome) -> ClusterState:
    """Give the state with the pool's machines and pods as the outcome has them, a new pool added beside it."""
    new_id = f"{NEW} {pool.id}"
    hourly = {offer.machine_type: offer.hourly for offer in search.offers}
    lines: Dict[str, List[Line]] = {pool.id: list(pool.lines), new_id: []}
    for machine in outcome.machines:
        known = {line.machine_type for line in lines[machine.pool]}
        if machine.machine_type is not None and machine.machine_type not in known:
            lines[machine.pool].append(Line(machine.machine_type, search.capacity, hourly[machine.machine_type], ""))
    pools: List[Pool] = []
    for each in state.pools:
        if each.id != pool.id:
            pools.append(each)
            continue
        pools.append(replace(pool, lines=tuple(lines[pool.id])))
        if lines[new_id]:
            pools.append(Pool(new_id, pool.name, f"{NEW} {pool.kind}", False, tuple(lines[new_id]), pool.id))
    machines = (*(machine for machine in state.machines if machine.pool != pool.id), *outcome.machines)
    by_name = {machine.name: machine for machine in outcome.machines}
    where = outcome.where
    return ClusterState(
        tuple(pools),
        tuple(sorted(machines, key=lambda machine: machine.name)),
        tuple(
            replace(pod, node=where[pod.key], pool=by_name[where[pod.key]].pool) if pod.key in where else pod
            for pod in state.pods
        ),
    )
