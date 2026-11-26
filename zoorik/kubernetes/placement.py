"""The one placement checker every step uses: where Kubernetes would let a pod run, and what each machine holds.

A pod fits a machine when its request fits what the machine has left, its node selection and tolerations allow the
machine, its DoNotSchedule spread holds as the scheduler counts it, its volumes reach the machine, its required pod
affinity and anti-affinity hold both ways, its host ports are free, and the machine runs the OS and CPU architecture
of the pod's machine today.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import AbstractSet, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple, Union

from zoorik.kubectl.objects import (
    DO_NOT_SCHEDULE,
    AffinityTerm,
    Container,
    HostPort,
    InitContainer,
    LabelSelector,
    NodeSelectorTerm,
    PersistentVolume,
    Requirement,
    SpreadConstraint,
    Taint,
    Toleration,
)
from zoorik.kubectl.objects import Pod as ApiPod
from zoorik.kubectl.quantity import CPU
from zoorik.kubernetes.model import MEMORY, PODS, Machine, Pod, Resources

HOSTNAME = "kubernetes.io/hostname"
OS_LABEL = "kubernetes.io/os"
ARCH_LABEL = "kubernetes.io/arch"
ZONE_LABEL = "topology.kubernetes.io/zone"
# The labels that come from a machine's type, by key and by key prefix.
TYPE_LABEL_KEYS = (
    "node.kubernetes.io/instance-type",
    "beta.kubernetes.io/instance-type",
    "kubernetes.azure.com/accelerator",
)
TYPE_LABEL_PREFIXES = (
    "karpenter.k8s.aws/instance-",
    "eks.amazonaws.com/instance-",
    "karpenter.azure.com/sku-",
    "feature.node.kubernetes.io/",
)
# Only these taint effects keep a pod off a machine; PreferNoSchedule is a preference.
BLOCKING_EFFECTS = ("NoSchedule", "NoExecute")
# The one field a node selector term's matchFields may name.
NODE_NAME_FIELD = "metadata.name"
# A spread constraint's node policies: nodeAffinityPolicy is Honor unless set to Ignore, nodeTaintsPolicy the other way.
IGNORE = "Ignore"
HONOR = "Honor"
# The resources a pod-level request may set, standing in for its containers' total.
POD_LEVEL_RESOURCES = (CPU, MEMORY)


# ---------------------------------------------------------------- a pod's request, as the scheduler counts it


def added(first: Resources, second: Resources) -> Dict[str, int]:
    """Add two resource lists."""
    return {key: first.get(key, 0) + second.get(key, 0) for key in {*first, *second}}


def largest(first: Resources, second: Resources) -> Dict[str, int]:
    """Keep the larger of two resource lists, resource by resource."""
    return {key: max(first.get(key, 0), second.get(key, 0)) for key in {*first, *second}}


def demand_of(pods: Sequence[Pod]) -> Dict[str, int]:
    """Total the pods' requests."""
    total: Dict[str, int] = {}
    for pod in pods:
        total = added(total, pod.request)
    return total


def every_container(pod: ApiPod) -> Tuple[Union[Container, InitContainer], ...]:
    """Give the pod's containers and init containers."""
    return (*pod.containers, *pod.init_containers)


def requests_of(pod: ApiPod) -> Dict[str, Resources]:
    """Give the requests of each of the pod's containers and init containers, by name, as the API server stores them."""
    return {container.name: container.requests for container in every_container(pod)}


def limits_of(pod: ApiPod) -> Dict[str, Resources]:
    """Give the limits of each of the pod's containers and init containers, by name."""
    return {container.name: container.limits for container in every_container(pod)}


def pod_total(pod: ApiPod, lists: Mapping[str, Resources], pod_level: Optional[Resources]) -> Dict[str, int]:
    """Total one list of each container's resources, by name, as Kubernetes totals a pod's requests or limits.

    It is the larger of what the containers and sidecars run with and what each init container starts with, beside the
    sidecars started before it. Pod-level resources stand in for the resources they set.
    """
    total: Dict[str, int] = {}
    for container in pod.containers:
        total = added(total, lists[container.name])
    sidecars: Dict[str, int] = {}
    starting: Dict[str, int] = {}
    for init in pod.init_containers:
        if init.sidecar:
            total = added(total, lists[init.name])
            sidecars = added(sidecars, lists[init.name])
            starting = largest(starting, sidecars)
        else:
            starting = largest(starting, added(lists[init.name], sidecars))
    total = largest(total, starting)
    if pod_level is not None:
        total.update({key: value for key, value in pod_level.items() if key in POD_LEVEL_RESOURCES})
    return total


def pod_request(pod: ApiPod, requests: Mapping[str, Resources], pod_level: Optional[Resources]) -> Dict[str, int]:
    """Give the pod's request as the scheduler counts it: its total, its overhead, and one pod slot."""
    return added(added(pod_total(pod, requests, pod_level), pod.overhead), {PODS: 1})


# ---------------------------------------------------------------- machine labels


def type_label(key: str) -> bool:
    """Tell whether a label comes from the machine's type."""
    return key in TYPE_LABEL_KEYS or key.startswith(TYPE_LABEL_PREFIXES)


def type_labels(nodes: Sequence[Mapping[str, str]]) -> Dict[str, str]:
    """Give the type labels a line's nodes all show with one value; a label they show apart names no one value."""
    shared: Optional[Dict[str, str]] = None
    for labels in nodes:
        own = {key: value for key, value in labels.items() if type_label(key)}
        shared = own if shared is None else {key: value for key, value in shared.items() if own.get(key) == value}
    return {} if shared is None else shared


def machine_labels(node: Mapping[str, str], line_type_labels: Mapping[str, str]) -> Dict[str, str]:
    """Give a machine's labels: its own, without type labels, and its type's labels as its line's nodes show them."""
    return {**{key: value for key, value in node.items() if not type_label(key)}, **line_type_labels}


# ---------------------------------------------------------------- selectors


def requirement_holds(requirement: Requirement, value: Optional[str]) -> bool:
    """Tell whether a label's value, None when the label is missing, meets one requirement."""
    operator = requirement.operator
    if operator in ("In", "NotIn"):
        found = value is not None and value in requirement.values
        return found if operator == "In" else not found
    if operator in ("Exists", "DoesNotExist"):
        return (value is not None) == (operator == "Exists")
    if operator in ("Gt", "Lt") and value is not None and len(requirement.values) == 1:
        return compares(operator, value, requirement.values[0])
    return False


def compares(operator: str, value: str, bound: str) -> bool:
    """Tell whether a label's value is greater (Gt) or less (Lt) than a bound, both as whole numbers."""
    try:
        number, limit = int(value), int(bound)
    except ValueError:
        # A value that is not a whole number meets neither comparison, as the API server decides.
        return False
    return number > limit if operator == "Gt" else number < limit


@dataclass(frozen=True)
class Selector:
    """A label selector, in a form that can key the counts a board keeps."""

    labels: Tuple[Tuple[str, str], ...]
    expressions: Tuple[Requirement, ...]

    def selects(self, labels: Mapping[str, str]) -> bool:
        """Tell whether the selector selects labels; an empty selector selects every set of labels."""
        if any(labels.get(key) != value for key, value in self.labels):
            return False
        return all(requirement_holds(item, labels.get(item.key)) for item in self.expressions)


def selector_of(
    selector: Optional[LabelSelector], match: Sequence[str], mismatch: Sequence[str], owner: Mapping[str, str]
) -> Optional[Selector]:
    """Read a label selector with its owner's values of matchLabelKeys added and of mismatchLabelKeys excluded.

    None selects no pods. A key the owner has no label for is left out, as the scheduler leaves it out.
    """
    if selector is None:
        return None
    added_keys = tuple(Requirement(key, "In", (owner[key],)) for key in match if key in owner)
    excluded = tuple(Requirement(key, "NotIn", (owner[key],)) for key in mismatch if key in owner)
    return Selector(tuple(sorted(selector.match_labels.items())), (*selector.match_expressions, *added_keys, *excluded))


def selected(selector: Optional[Selector], labels: Mapping[str, str]) -> bool:
    """Tell whether a selector, which may select nothing, selects labels."""
    return selector is not None and selector.selects(labels)


# ---------------------------------------------------------------- the rules that need no other pod


def term_matches(term: NodeSelectorTerm, labels: Mapping[str, str], name: str) -> bool:
    """Tell whether a machine meets one node selector term; an empty term meets nothing."""
    if not term.match_expressions and not term.match_fields:
        return False
    fields_hold = all(
        requirement_holds(item, name if item.key == NODE_NAME_FIELD else None) for item in term.match_fields
    )
    return fields_hold and all(requirement_holds(item, labels.get(item.key)) for item in term.match_expressions)


def node_selected(pod: ApiPod, labels: Mapping[str, str], name: str) -> bool:
    """Tell whether the pod's node selector and required node affinity allow a machine."""
    if any(labels.get(key) != value for key, value in pod.node_selector.items()):
        return False
    return pod.node_affinity is None or any(term_matches(term, labels, name) for term in pod.node_affinity)


def tolerates(toleration: Toleration, taint: Taint) -> bool:
    """Tell whether one toleration tolerates one taint, as the scheduler decides."""
    if toleration.effect and toleration.effect != taint.effect:
        return False
    if toleration.key and toleration.key != taint.key:
        return False
    if toleration.operator == "Exists":
        return True
    # A missing operator means Equal, and a missing value is the empty value.
    return toleration.operator in (None, "Equal") and (toleration.value or "") == (taint.value or "")


def tolerated(tolerations: Sequence[Toleration], taints: Sequence[Taint]) -> bool:
    """Tell whether the tolerations cover every NoSchedule and NoExecute taint."""
    blocking = (taint for taint in taints if taint.effect in BLOCKING_EFFECTS)
    return all(any(tolerates(toleration, taint) for toleration in tolerations) for taint in blocking)


def volumes_reach(volumes: Sequence[PersistentVolume], labels: Mapping[str, str], name: str) -> bool:
    """Tell whether every volume's required node affinity, a zone or a node, allows the machine."""
    return all(
        volume.node_affinity is None or any(term_matches(term, labels, name) for term in volume.node_affinity)
        for volume in volumes
    )


def same_platform(pod: Pod, labels: Mapping[str, str]) -> bool:
    """Tell whether the machine runs the OS and CPU architecture of the pod's machine today, which its images need."""
    return labels.get(OS_LABEL) == pod.os and labels.get(ARCH_LABEL) == pod.arch


def host_ports(pod: ApiPod) -> FrozenSet[HostPort]:
    """Name the host ports the pod's containers take on their machine."""
    return frozenset(port for container in pod.containers for port in container.host_ports)


@dataclass
class Room:
    """What a machine has left for pods: its free resources, the host ports taken, and its volumes by CSI driver."""

    free: Dict[str, int]
    ports: Set[HostPort]
    volumes: Dict[str, Set[str]] = field(default_factory=dict)

    def take(self, pod: Pod) -> None:
        """Give the machine's room to a pod."""
        for resource, amount in pod.request.items():
            self.free[resource] = self.free.get(resource, 0) - amount
        self.ports.update(host_ports(pod.source))
        for volume in pod.volumes:
            if volume.driver is not None:
                self.volumes.setdefault(volume.driver, set()).add(volume.name)


def resources_fit(request: Resources, room: Room) -> bool:
    """Tell whether a request fits the machine's free resources, every requested resource counted."""
    return all(amount <= room.free.get(resource, 0) for resource, amount in request.items())


def attach_fits(volumes: Sequence[PersistentVolume], attach: Mapping[str, int], room: Room) -> bool:
    """Tell whether the pod's volumes fit each CSI driver's attach limit on the machine, a shared volume once."""
    for driver, limit in attach.items():
        attached = room.volumes.get(driver, set())
        new = {volume.name for volume in volumes if volume.driver == driver} - attached
        if len(attached) + len(new) > limit:
            return False
    return True


def fits_machine(pod: Pod, machine: Machine, room: Room) -> bool:
    """Tell whether the pod fits the machine by the rules that need no other pod: all but spread and pod affinity."""
    return (
        resources_fit(pod.request, room)
        and attach_fits(pod.volumes, machine.attach, room)
        and node_selected(pod.source, machine.labels, machine.name)
        and tolerated(pod.source.tolerations, machine.taints)
        and volumes_reach(pod.volumes, machine.labels, machine.name)
        and not host_ports(pod.source) & room.ports
        and same_platform(pod, machine.labels)
    )


# ---------------------------------------------------------------- machine overhead


@dataclass(frozen=True)
class Overhead:
    """What every machine of a pool gives its DaemonSets and static pods: resources, pod slots and host ports."""

    requests: Resources
    ports: FrozenSet[HostPort]


def pool_overhead(pods: Sequence[Pod]) -> Overhead:
    """Work out a pool's machine overhead from its DaemonSet and static pods.

    It is the request of every DaemonSet with a pod in the pool (its largest pod's), plus the largest static-pod total
    on one machine; each pod's request holds its pod slot.
    """
    daemons: Dict[Tuple[str, str], Resources] = {}
    statics: Dict[str, Resources] = {}
    ports: Set[HostPort] = set()
    for pod in pods:
        if pod.source.static:
            statics[pod.node] = added(statics.get(pod.node, {}), pod.request)
        else:
            key = (pod.workload.namespace, pod.workload.name)
            daemons[key] = largest(daemons.get(key, {}), pod.request)
        ports.update(host_ports(pod.source))
    total: Resources = {}
    for request in daemons.values():
        total = added(total, request)
    static_most: Resources = {}
    for request in statics.values():
        static_most = largest(static_most, request)
    return Overhead(added(total, static_most), frozenset(ports))


def empty_room(machine: Machine, overhead: Overhead) -> Room:
    """Give an empty machine's room: its allocatable, less its pool's overhead."""
    free = {resource: amount - overhead.requests.get(resource, 0) for resource, amount in machine.allocatable.items()}
    return Room(free, set(overhead.ports))


# ---------------------------------------------------------------- what keeps a pod on its node


def selects_namespaces_by_label(pod: ApiPod) -> bool:
    """Tell whether a required affinity term selects namespaces by their labels, which the run doesn't read."""
    return any(
        term.namespace_selector is not None
        and bool(term.namespace_selector.match_labels or term.namespace_selector.match_expressions)
        for term in (*pod.affinity, *pod.anti_affinity)
    )


def bound_to_one_node(volume: PersistentVolume) -> bool:
    """Tell whether a volume's required node affinity names a node, as a local volume's does."""
    return volume.node_affinity is not None and any(
        item.key == HOSTNAME for term in volume.node_affinity for item in term.match_expressions
    )


# ---------------------------------------------------------------- the rules that count the pods already placed


@dataclass(frozen=True)
class Term:
    """A required affinity term as it applies: the pods it selects, the namespaces it covers, and its topology key."""

    selector: Optional[Selector]
    # None covers every namespace.
    namespaces: Optional[FrozenSet[str]]
    key: str

    def covers(self, pod: ApiPod) -> bool:
        """Tell whether the term selects a pod."""
        return (self.namespaces is None or pod.namespace in self.namespaces) and selected(self.selector, pod.labels)


def term_of(term: AffinityTerm, owner: ApiPod) -> Term:
    """Read a pod's affinity term, its owner's matchLabelKeys and mismatchLabelKeys applied.

    A term with a namespace selector covers every namespace: an empty selector selects them all, and the run can't read
    a non-empty one, so it is taken to cover them all, which only ever rules out more (its own pod stays on its node).
    """
    selector = selector_of(term.label_selector, term.match_label_keys, term.mismatch_label_keys, owner.labels)
    if term.namespace_selector is not None:
        return Term(selector, None, term.topology_key)
    return Term(selector, frozenset(term.namespaces or (owner.namespace,)), term.topology_key)


@dataclass(frozen=True)
class Placed:
    """A pod and the machine it is on."""

    pod: Pod
    machine: Machine


@dataclass
class Count:
    """One spread constraint's eligible machines, their domains, and the matching pods in each, kept as pods go."""

    names: FrozenSet[str]
    domains: FrozenSet[str]
    # A domain no matching pod is in holds none.
    pods: Dict[str, int]


@dataclass(frozen=True)
class Spread:
    """One DoNotSchedule constraint as it stands for a pod: the matching pods per domain, and the global minimum."""

    key: str
    max_skew: int
    pods: Mapping[str, int]
    lowest: int
    # 1 when the pod's own labels match the constraint, so placing it adds to its domain.
    own: int

    def allows(self, labels: Mapping[str, str]) -> bool:
        """Tell whether placing the pod on a machine with labels keeps the skew within its maximum."""
        value = labels.get(self.key)
        return value is not None and self.pods.get(value, 0) + self.own - self.lowest <= self.max_skew


@dataclass(frozen=True)
class Affinity:
    """The pod's required pod affinity as it stands: the domains where a pod matching all its terms runs."""

    keys: Tuple[str, ...]
    pairs: AbstractSet[Tuple[str, str]]
    # No pod matches all its terms and the pod matches them itself, so the first pod of a group may go anywhere.
    first: bool

    def allows(self, labels: Mapping[str, str]) -> bool:
        """Tell whether a machine with labels is in a domain of every term."""
        if any(key not in labels for key in self.keys):
            return False
        return self.first or all((key, labels[key]) in self.pairs for key in self.keys)


@dataclass(frozen=True)
class Rules:
    """The rules that count placed pods, as they stand for one pod."""

    spreads: Tuple[Spread, ...]
    affinity: Optional[Affinity]
    # The domains the pod may not join: by topology key, the values its anti-affinity or a placed pod's rules out.
    forbidden: Mapping[str, AbstractSet[str]]

    def allow(self, machine: Machine) -> bool:
        """Tell whether these rules allow the machine."""
        labels = machine.labels
        if any(labels.get(key) in values for key, values in self.forbidden.items()):
            return False
        if self.affinity is not None and not self.affinity.allows(labels):
            return False
        return all(spread.allows(labels) for spread in self.spreads)


# A spread count's key: the pod's namespace, the constraint's selector and topology key, and who is eligible.
SpreadKey = Tuple[str, Optional[Selector], str, Tuple[object, ...]]


class Board:
    """One pass of a packing: the machines it counts, the machines open to pods, and every pod placed so far.

    Spread counts the domains of every machine in the universe, empty ones included. Only opened machines take pods'
    room, each after its pool's overhead. The counts each rule needs are kept per domain and updated as pods are placed.
    """

    def __init__(self, universe: Sequence[Machine], overheads: Mapping[str, Overhead]) -> None:
        """Count domains over universe; each opened machine gives its pool's overhead first."""
        self._universe = tuple(universe)
        self._overheads = overheads
        self._rooms: Dict[str, Room] = {}
        self._placed: List[Placed] = []
        self._by_namespace: Dict[str, List[Placed]] = {}
        self._eligible: Dict[Tuple[object, ...], Tuple[Machine, ...]] = {}
        self._spreads: Dict[str, Dict[SpreadKey, Count]] = {}
        self._affinities: Dict[Tuple[Term, ...], Set[Tuple[str, str]]] = {}
        self._anti: Dict[Term, Set[str]] = {}
        # Placed pods' anti-affinity terms, by each namespace they cover; None holds those that cover every namespace.
        self._holders: Dict[Optional[str], List[Tuple[Term, Placed]]] = {}
        self.where: Dict[str, str] = {}

    def open(self, machine: Machine) -> None:
        """Let pods go on machine."""
        self._rooms[machine.name] = empty_room(machine, self._overheads[machine.pool])

    def place(self, pod: Pod, machine: Machine) -> None:
        """Put pod on machine, which takes its room if it is opened, and add it to every count it matches."""
        placed = Placed(pod, machine)
        self._placed.append(placed)
        self._by_namespace.setdefault(pod.source.namespace, []).append(placed)
        self._count(placed)
        for term in (term_of(item, pod.source) for item in pod.source.anti_affinity):
            for namespace in (None,) if term.namespaces is None else sorted(term.namespaces):
                self._holders.setdefault(namespace, []).append((term, placed))
        room = self._rooms.get(machine.name)
        if room is not None:
            room.take(pod)
        self.where[pod.key] = machine.name

    def fits(self, pod: Pod, machine: Machine, rules: Rules) -> bool:
        """Tell whether pod fits machine by every rule; a machine not opened is taken as empty."""
        room = self._rooms.get(machine.name)
        if room is None:
            room = empty_room(machine, self._overheads[machine.pool])
        return fits_machine(pod, machine, room) and rules.allow(machine)

    def rules(self, pod: Pod) -> Rules:
        """Work out the rules that count placed pods, as they stand now, for pod."""
        constraints = [item for item in pod.source.spread if item.when_unsatisfiable == DO_NOT_SCHEDULE]
        keys = tuple(sorted({item.topology_key for item in constraints}))
        spreads = tuple(self._spread(pod.source, constraint, keys) for constraint in constraints)
        affinity = self._affinity(pod.source) if pod.source.affinity else None
        return Rules(spreads, affinity, self._forbidden(pod.source))

    def _count(self, placed: Placed) -> None:
        """Add a placed pod to every kept count it matches."""
        source, labels = placed.pod.source, placed.machine.labels
        for (_, selector, key, _), count in self._spreads.get(source.namespace, {}).items():
            if placed.machine.name in count.names and selected(selector, source.labels):
                count.pods[labels[key]] = count.pods.get(labels[key], 0) + 1
        for terms, pairs in self._affinities.items():
            if all(term.covers(source) for term in terms):
                pairs.update((term.key, labels[term.key]) for term in terms if term.key in labels)
        for term, values in self._anti.items():
            if term.key in labels and term.covers(source):
                values.add(labels[term.key])

    def _eligible_machines(
        self, pod: ApiPod, constraint: SpreadConstraint, keys: Tuple[str, ...]
    ) -> Tuple[Tuple[object, ...], Tuple[Machine, ...]]:
        """Give the universe's machines a spread constraint counts, and the key they are kept under.

        A machine counts when it has every DoNotSchedule topology key of the pod and passes the constraint's node
        policies: the pod's node selection unless nodeAffinityPolicy is Ignore, and its tolerations when
        nodeTaintsPolicy is Honor.
        """
        honor_affinity = constraint.node_affinity_policy != IGNORE
        honor_taints = constraint.node_taints_policy == HONOR
        key = (
            keys,
            tuple(sorted(pod.node_selector.items())) if honor_affinity else None,
            pod.node_affinity if honor_affinity else None,
            pod.tolerations if honor_taints else None,
        )
        if key not in self._eligible:
            self._eligible[key] = tuple(
                machine
                for machine in self._universe
                if all(name in machine.labels for name in keys)
                and (not honor_affinity or node_selected(pod, machine.labels, machine.name))
                and (not honor_taints or tolerated(pod.tolerations, machine.taints))
            )
        return key, self._eligible[key]

    def _spread(self, pod: ApiPod, constraint: SpreadConstraint, keys: Tuple[str, ...]) -> Spread:
        """Give one DoNotSchedule constraint's counts for pod, counting them from the placed pods the first time."""
        selector = selector_of(constraint.label_selector, constraint.match_label_keys, (), pod.labels)
        eligibility, machines = self._eligible_machines(pod, constraint, keys)
        topology = constraint.topology_key
        counts = self._spreads.setdefault(pod.namespace, {})
        count_key = (pod.namespace, selector, topology, eligibility)
        count = counts.get(count_key)
        if count is None:
            names = frozenset(machine.name for machine in machines)
            matching = Counter(
                placed.machine.labels[topology]
                for placed in self._by_namespace.get(pod.namespace, ())
                if placed.machine.name in names and selected(selector, placed.pod.source.labels)
            )
            count = Count(names, frozenset(machine.labels[topology] for machine in machines), matching)
            counts[count_key] = count
        min_domains = 1 if constraint.min_domains is None else constraint.min_domains
        # With fewer eligible domains than minDomains, the scheduler takes the global minimum as 0.
        lowest = 0
        if len(count.domains) >= min_domains:
            lowest = min((count.pods.get(domain, 0) for domain in count.domains), default=0)
        own = 1 if selected(selector, pod.labels) else 0
        return Spread(topology, constraint.max_skew, count.pods, lowest, own)

    def _affinity(self, pod: ApiPod) -> Affinity:
        """Give the domains where a placed pod matches every required affinity term of pod."""
        terms = tuple(term_of(item, pod) for item in pod.affinity)
        pairs = self._affinities.get(terms)
        if pairs is None:
            pairs = {
                (term.key, placed.machine.labels[term.key])
                for placed in self._placed
                if all(each.covers(placed.pod.source) for each in terms)
                for term in terms
                if term.key in placed.machine.labels
            }
            self._affinities[terms] = pairs
        first = not pairs and all(term.covers(pod) for term in terms)
        return Affinity(tuple(term.key for term in terms), pairs, first)

    def _forbidden(self, pod: ApiPod) -> Dict[str, Set[str]]:
        """Give the domains pod may not join, by topology key.

        Those are the domains of the pods its own anti-affinity selects, and of the placed pods whose anti-affinity
        selects it.
        """
        forbidden: Dict[str, Set[str]] = {}
        for term in (term_of(item, pod) for item in pod.anti_affinity):
            values = self._anti.get(term)
            if values is None:
                values = {
                    placed.machine.labels[term.key]
                    for placed in self._placed
                    if term.key in placed.machine.labels and term.covers(placed.pod.source)
                }
                self._anti[term] = values
            forbidden.setdefault(term.key, set()).update(values)
        for term, placed in (*self._holders.get(pod.namespace, ()), *self._holders.get(None, ())):
            if term.key in placed.machine.labels and term.covers(pod):
                forbidden.setdefault(term.key, set()).add(placed.machine.labels[term.key])
        return forbidden
