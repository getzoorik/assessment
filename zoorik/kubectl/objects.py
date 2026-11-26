"""The allowlist: the only fields the run reads of each kind, the typed objects read from them, and the jq filter.

One allowlist serves both read methods. The direct read keeps each page's allowed fields with slim_page; the
command-invoke read runs jq_filter, which is generated from the same rules, inside the cluster. Both then read what was
kept into the typed objects below. Env values, images, commands, annotations and every other field are never read.
"""

import string
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple, Union

from zoorik.cloud.fields import Record
from zoorik.errors import UnexpectedResponse
from zoorik.kubectl.quantity import quantity, resources

# A JSON value as the API sends it.
Json = Any

# The table of jq's ascii_downcase, so that both readers fold a key the same way.
ASCII_DOWNCASE = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
REQUIRED = "requiredDuringSchedulingIgnoredDuringExecution"
DO_NOT_SCHEDULE = "DoNotSchedule"
# The platform marks its own add-ons with this label, which a later step reads.
ADDON_MODE = "addonmanager.kubernetes.io/mode"
# A pod in either phase has ended and holds no resources.
FINISHED = ("Succeeded", "Failed")
# An init container that restarts Always is a sidecar: it runs beside the containers for the pod's whole life.
SIDECAR_POLICY = "Always"
# The disk tier sits under skuname or its documented alias storageaccounttype, in any case.
TIER_KEYS = ("skuname", "storageaccounttype")
# An in-tree volume type, and the CSI driver that serves it since the CSI migration.
IN_TREE_DRIVERS = (("azureDisk", "disk.csi.azure.com"), ("awsElasticBlockStore", "ebs.csi.aws.com"))


@dataclass(frozen=True)
class Whole:
    """Keep the value as it is."""


@dataclass(frozen=True)
class Fields:
    """Keep only these keys of an object, each by its own rule; a key left out or null stays out."""

    rules: Mapping[str, "Rule"]


@dataclass(frozen=True)
class Each:
    """Keep each item of a list by one rule; with where, only the items whose where field is set and not false."""

    item: "Rule"
    where: Optional[str] = None


@dataclass(frozen=True)
class FoldedKeys:
    """Keep only the keys of an object that, with ASCII case ignored, are one of names; their values stay whole."""

    names: Tuple[str, ...]


@dataclass(frozen=True)
class PlacementLabels:
    """A pod's labels: kept whole in a page, then narrowed across pods to the keys a required placement term uses."""


Rule = Union[Whole, Fields, Each, FoldedKeys, PlacementLabels]
WHOLE = Whole()


def dig(value: Json, path: Sequence[str]) -> Json:
    """Give the value at path inside nested objects, or None where a step is missing."""
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def jq_path(path: Sequence[str]) -> str:
    """Write path as a jq path, such as .status.phase."""
    return "." + ".".join(path)


@dataclass(frozen=True)
class Without:
    """An item passes unless the value at path is one of values."""

    path: Tuple[str, ...]
    values: Tuple[str, ...]

    def passes(self, item: Json) -> bool:
        """Tell whether item passes."""
        return dig(item, self.path) not in self.values

    def jq(self) -> str:
        """Write the same test in jq."""
        return " and ".join(f'{jq_path(self.path)} != "{value}"' for value in self.values)


@dataclass(frozen=True)
class NonEmpty:
    """An item passes when the list at path holds something."""

    path: Tuple[str, ...]

    def passes(self, item: Json) -> bool:
        """Tell whether item passes."""
        return bool(dig(item, self.path))

    def jq(self) -> str:
        """Write the same test in jq."""
        return f"({jq_path(self.path)} // []) | length > 0"


ItemTest = Union[Without, NonEmpty]


@dataclass(frozen=True)
class Kind:
    """One kind the run lists: its name, kubectl resource and API path, its items' kind, rule and test."""

    name: str
    resource: str
    path: str
    item_kind: str
    item: Rule
    # The test an item must pass, after its rule, to be kept; None keeps every item.
    test: Optional[ItemTest] = None

    @property
    def list_kind(self) -> str:
        """Name the kind of a list of these items, such as PodList."""
        return f"{self.item_kind}List"


# Only the controller owner reference is read: the one object that recreates the pod or owns the object.
OWNERS = Each(Fields({"kind": WHOLE, "name": WHOLE, "controller": WHOLE}), where="controller")
RESOURCE_LISTS = Fields({"requests": WHOLE, "limits": WHOLE})
AFFINITY_TERM = Fields(
    {
        key: WHOLE
        for key in (
            "labelSelector",
            "namespaces",
            "namespaceSelector",
            "topologyKey",
            "matchLabelKeys",
            "mismatchLabelKeys",
        )
    }
)
NODE = Fields(
    {
        "metadata": Fields({"name": WHOLE, "labels": WHOLE}),
        "spec": Fields({"providerID": WHOLE, "taints": Each(Fields({"key": WHOLE, "value": WHOLE, "effect": WHOLE}))}),
        "status": Fields({"capacity": WHOLE, "allocatable": WHOLE, "nodeInfo": Fields({"kubeletVersion": WHOLE})}),
    }
)
POD = Fields(
    {
        "metadata": Fields({"name": WHOLE, "namespace": WHOLE, "ownerReferences": OWNERS, "labels": PlacementLabels()}),
        "spec": Fields(
            {
                "nodeName": WHOLE,
                "overhead": WHOLE,
                "resources": RESOURCE_LISTS,
                "containers": Each(
                    Fields(
                        {
                            "name": WHOLE,
                            "resources": RESOURCE_LISTS,
                            "ports": Each(Fields({"hostPort": WHOLE, "protocol": WHOLE}), where="hostPort"),
                        }
                    )
                ),
                "initContainers": Each(Fields({"name": WHOLE, "resources": RESOURCE_LISTS, "restartPolicy": WHOLE})),
                "nodeSelector": WHOLE,
                "affinity": Fields(
                    {
                        "nodeAffinity": Fields({REQUIRED: WHOLE}),
                        "podAffinity": Fields({REQUIRED: Each(AFFINITY_TERM)}),
                        "podAntiAffinity": Fields({REQUIRED: Each(AFFINITY_TERM)}),
                    }
                ),
                "tolerations": WHOLE,
                "topologySpreadConstraints": WHOLE,
                "volumes": Each(
                    Fields({"persistentVolumeClaim": Fields({"claimName": WHOLE})}), where="persistentVolumeClaim"
                ),
            }
        ),
        "status": Fields({"phase": WHOLE}),
    }
)
OWNED = Fields({"metadata": Fields({"name": WHOLE, "namespace": WHOLE, "ownerReferences": OWNERS})})
CLAIM = Fields({"metadata": Fields({"name": WHOLE, "namespace": WHOLE}), "spec": Fields({"volumeName": WHOLE})})
VOLUME = Fields(
    {
        "metadata": Fields({"name": WHOLE}),
        "spec": Fields(
            {
                "nodeAffinity": Fields({"required": WHOLE}),
                "csi": Fields({"driver": WHOLE, "volumeAttributes": FoldedKeys(TIER_KEYS)}),
                # Only whether an in-tree disk is there: its type names its driver.
                "azureDisk": Fields({}),
                "awsElasticBlockStore": Fields({}),
            }
        ),
    }
)
CSI_NODE = Fields(
    {
        "metadata": Fields({"name": WHOLE}),
        "spec": Fields({"drivers": Each(Fields({"name": WHOLE, "allocatable": Fields({"count": WHOLE})}))}),
    }
)
LIMIT_RANGE = Fields(
    {
        "metadata": Fields({"namespace": WHOLE}),
        "spec": Fields(
            {"limits": Each(Fields({"type": WHOLE, "min": WHOLE, "max": WHOLE, "maxLimitRequestRatio": WHOLE}))}
        ),
    }
)


NODES = Kind("nodes", "nodes", "/api/v1/nodes", "Node", NODE)
PODS = Kind("pods", "pods", "/api/v1/pods", "Pod", POD, Without(("status", "phase"), FINISHED))
# A ReplicaSet or Job with no controller leads nowhere above itself, so only those with one are kept.
REPLICASETS = Kind(
    "replicasets",
    "replicasets.apps",
    "/apis/apps/v1/replicasets",
    "ReplicaSet",
    OWNED,
    NonEmpty(("metadata", "ownerReferences")),
)
JOBS = Kind("jobs", "jobs.batch", "/apis/batch/v1/jobs", "Job", OWNED, NonEmpty(("metadata", "ownerReferences")))
CLAIMS = Kind(
    "persistentvolumeclaims",
    "persistentvolumeclaims",
    "/api/v1/persistentvolumeclaims",
    "PersistentVolumeClaim",
    CLAIM,
)
VOLUMES = Kind("persistentvolumes", "persistentvolumes", "/api/v1/persistentvolumes", "PersistentVolume", VOLUME)
CSI_NODES = Kind("csinodes", "csinodes.storage.k8s.io", "/apis/storage.k8s.io/v1/csinodes", "CSINode", CSI_NODE)
LIMIT_RANGES = Kind("limitranges", "limitranges", "/api/v1/limitranges", "LimitRange", LIMIT_RANGE)
KINDS = (NODES, PODS, REPLICASETS, JOBS, CLAIMS, VOLUMES, CSI_NODES, LIMIT_RANGES)


# ---------------------------------------------------------------- keeping the allowed fields


def slim(value: Json, rule: Rule, path: str) -> Json:
    """Keep only the parts of value that rule allows; a value of another shape than rule expects is unexpected."""
    if isinstance(rule, (Whole, PlacementLabels)):
        return value
    if isinstance(rule, Each):
        items = enumerate(_items(value, path))
        return [slim(item, rule.item, f"{path}[{n}]") for n, item in items if _kept(item, rule.where, f"{path}[{n}]")]
    if isinstance(rule, FoldedKeys):
        return {key: item for key, item in _fields(value, path).items() if key.translate(ASCII_DOWNCASE) in rule.names}
    fields = _fields(value, path)
    return {
        key: slim(fields[key], child, f"{path}.{key}")
        for key, child in rule.rules.items()
        if fields.get(key) is not None
    }


def _fields(value: Json, path: str) -> Dict[str, Json]:
    """Give value as an object, or raise UnexpectedResponse naming path."""
    if not isinstance(value, dict):
        raise UnexpectedResponse(f"unexpected answer: {path} is not an object")
    return value


def _items(value: Json, path: str) -> List[Json]:
    """Give value as a list, or raise UnexpectedResponse naming path."""
    if not isinstance(value, list):
        raise UnexpectedResponse(f"unexpected answer: {path} is not a list")
    return value


def _kept(item: Json, where: Optional[str], path: str) -> bool:
    """Tell whether a list item is kept: always without where, else when its where field is set and not false."""
    if where is None:
        return True
    found = _fields(item, path).get(where)
    # jq's test: anything but null and false passes, 0 included.
    return found is not None and found is not False


@dataclass(frozen=True)
class Page:
    """One page of a list, slimmed: the items kept, and the token for the next page (None on the last)."""

    items: List[Json]
    token: Optional[str]


def slim_page(document: Json, kind: Kind) -> Page:
    """Keep of one page of kind's list the allowed fields of the items that pass kind's test, and the next token."""
    page = Record(document, kind.resource)
    found = page.text("kind")
    if found != kind.list_kind:
        raise UnexpectedResponse(f"unexpected answer: kubectl gave a {found} for {kind.resource}")
    metadata = page.optional_record("metadata")
    token = None if metadata is None else metadata.optional_text("continue")
    raw = _items(_fields(document, kind.resource).get("items"), f"{kind.resource}.items")
    items = [slim(item, kind.item, f"{kind.resource}[{index}]") for index, item in enumerate(raw)]
    return Page([item for item in items if kind.test is None or kind.test.passes(item)], token or None)


# ---------------------------------------------------------------- the same rules in jq, for the command-invoke read

# k keeps one key by a filter when it is set; e keeps the list items whose where field is set; sk names a label
# selector's keys.
JQ_HELPERS = (
    "def k(n; f): n as $n | if .[$n] != null then {($n): (.[$n] | f)} else {} end; "
    "def e(w; f): w as $w | map(select(.[$w]) | f); "
    "def sk: ((.matchLabels // {}) | keys) + [(.matchExpressions // [])[] | .key];"
)
# The label keys a required placement term of any pod in $p uses, plus the add-on manager's mode, as a set.
PLACEMENT_KEYS_JQ = (
    "[$p[] | .spec | ((.topologySpreadConstraints // [])[] "
    f'| select(.whenUnsatisfiable == "{DO_NOT_SCHEDULE}") '
    "| (((.labelSelector // {}) | sk) + (.matchLabelKeys // []))[]), "
    f"(((.affinity.podAffinity.{REQUIRED} // []) + (.affinity.podAntiAffinity.{REQUIRED} // []))[] "
    "| (((.labelSelector // {}) | sk) + (.matchLabelKeys // []) + (.mismatchLabelKeys // []))[])] "
    f'+ ["{ADDON_MODE}"] | map({{(.): true}}) | add'
)
NARROW_LABELS_JQ = "if .metadata.labels != null then .metadata.labels |= with_entries(select($keep[.key])) else . end"


def jq(rule: Rule) -> str:
    """Write rule as a jq filter that keeps what slim keeps."""
    if isinstance(rule, (Whole, PlacementLabels)):
        return "."
    if isinstance(rule, Each):
        return f"map({jq(rule.item)})" if rule.where is None else f'e("{rule.where}"; {jq(rule.item)})'
    if isinstance(rule, FoldedKeys):
        names = " or ".join(f'. == "{name}"' for name in rule.names)
        return f"with_entries(select(.key | ascii_downcase | {names}))"
    if not rule.rules:
        return "{}"
    return "(" + " + ".join(f'k("{key}"; {jq(child)})' for key, child in rule.rules.items()) + ")"


def jq_filter(kind: Kind) -> str:
    """Write the jq program that turns kubectl get's answer for kind into what slim_page keeps, pod labels narrowed.

    The pods' labels are narrowed inside the cluster, as with_placement_labels narrows them, so the answer stays small.
    """
    test = "" if kind.test is None else f" | select({kind.test.jq()})"
    items = f"[.items[] | {jq(kind.item)}{test}]"
    if kind is not PODS:
        return f"{JQ_HELPERS} {items}"
    return f"{JQ_HELPERS} {items} as $p | ({PLACEMENT_KEYS_JQ}) as $keep | [$p[] | {NARROW_LABELS_JQ}]"


# ---------------------------------------------------------------- the typed objects, read from what was kept


@dataclass(frozen=True)
class Taint:
    """A node taint."""

    key: str
    value: Optional[str]
    effect: str


@dataclass(frozen=True)
class Node:
    """A node: its labels and taints, and its capacity and allocatable in scheduler units."""

    name: str
    labels: Mapping[str, str]
    taints: Tuple[Taint, ...]
    # spec.providerID, which a machine of a cloud carries.
    provider_id: Optional[str]
    capacity: Mapping[str, int]
    allocatable: Mapping[str, int]
    kubelet_version: str


@dataclass(frozen=True)
class OwnerRef:
    """An owner, by kind and name, in its object's namespace."""

    kind: str
    name: str


@dataclass(frozen=True)
class HostPort:
    """A port a container takes on its node."""

    port: int
    protocol: str


@dataclass(frozen=True)
class Container:
    """A container's requests and limits as the API server stores them, in scheduler units, and its host ports."""

    name: str
    requests: Mapping[str, int]
    limits: Mapping[str, int]
    host_ports: Tuple[HostPort, ...]


@dataclass(frozen=True)
class InitContainer:
    """An init container's requests and limits, and whether it is a sidecar."""

    name: str
    requests: Mapping[str, int]
    limits: Mapping[str, int]
    sidecar: bool


@dataclass(frozen=True)
class Requirement:
    """One requirement of a selector: a key, an operator and the values it compares with."""

    key: str
    operator: str
    values: Tuple[str, ...]


@dataclass(frozen=True)
class LabelSelector:
    """A label selector: labels that must match, and requirements."""

    match_labels: Mapping[str, str]
    match_expressions: Tuple[Requirement, ...]


@dataclass(frozen=True)
class NodeSelectorTerm:
    """One term of a required node affinity; a node must meet every requirement of one term."""

    match_expressions: Tuple[Requirement, ...]
    match_fields: Tuple[Requirement, ...]


@dataclass(frozen=True)
class AffinityTerm:
    """One required pod affinity or anti-affinity term."""

    # None selects no pods.
    label_selector: Optional[LabelSelector]
    namespaces: Tuple[str, ...]
    # None means the term's namespaces alone; an empty selector means every namespace.
    namespace_selector: Optional[LabelSelector]
    topology_key: str
    match_label_keys: Tuple[str, ...]
    mismatch_label_keys: Tuple[str, ...]


@dataclass(frozen=True)
class Toleration:
    """A toleration; each part left out means what the API documents for it."""

    key: Optional[str]
    operator: Optional[str]
    value: Optional[str]
    effect: Optional[str]


@dataclass(frozen=True)
class SpreadConstraint:
    """A topology spread constraint."""

    max_skew: int
    topology_key: str
    when_unsatisfiable: str
    label_selector: Optional[LabelSelector]
    min_domains: Optional[int]
    node_affinity_policy: Optional[str]
    node_taints_policy: Optional[str]
    match_label_keys: Tuple[str, ...]


@dataclass(frozen=True)
class Pod:
    """A pod that has not ended: where it runs, what it asks for, and what limits where it may run."""

    namespace: str
    name: str
    # The controller owner reference; None for a pod nothing recreates.
    controller: Optional[OwnerRef]
    # None for a pod not placed yet.
    node: Optional[str]
    # Only the labels whose keys a required placement term uses, and the add-on manager's mode.
    labels: Mapping[str, str]
    overhead: Mapping[str, int]
    # The pod-level resources; None when the pod sets none.
    requests: Optional[Mapping[str, int]]
    limits: Optional[Mapping[str, int]]
    containers: Tuple[Container, ...]
    init_containers: Tuple[InitContainer, ...]
    node_selector: Mapping[str, str]
    # The terms of the required node affinity, one of which a node must meet; None when the pod has none.
    node_affinity: Optional[Tuple[NodeSelectorTerm, ...]]
    affinity: Tuple[AffinityTerm, ...]
    anti_affinity: Tuple[AffinityTerm, ...]
    tolerations: Tuple[Toleration, ...]
    spread: Tuple[SpreadConstraint, ...]
    # The names of the claims its volumes use.
    claims: Tuple[str, ...]

    @property
    def static(self) -> bool:
        """Tell whether the pod is a static pod: its controller is a node, as a mirror pod's is."""
        return self.controller is not None and self.controller.kind == "Node"


@dataclass(frozen=True)
class Owned:
    """A ReplicaSet or Job and its controller, which lead from a pod to its workload."""

    kind: str
    namespace: str
    name: str
    controller: OwnerRef


@dataclass(frozen=True)
class PersistentVolumeClaim:
    """A claim and the volume bound to it."""

    namespace: str
    name: str
    # None for a claim not bound yet.
    volume_name: Optional[str]


@dataclass(frozen=True)
class PersistentVolume:
    """A volume: its CSI driver, its disk tier and the nodes it can reach."""

    name: str
    # The CSI driver, or for an in-tree disk the driver that serves its type; None for any other volume.
    driver: Optional[str]
    # The disk tier, such as Premium_LRS; None when the volume names none.
    tier: Optional[str]
    # The terms of its required node affinity, one of which a node must meet; None when it has none.
    node_affinity: Optional[Tuple[NodeSelectorTerm, ...]]


@dataclass(frozen=True)
class CsiNode:
    """The CSI drivers on a node, and how many volumes each can attach there; None where a driver gives no limit."""

    name: str
    drivers: Mapping[str, Optional[int]]


@dataclass(frozen=True)
class LimitItem:
    """One limit of a LimitRange, for a Container, a Pod or a PersistentVolumeClaim."""

    type: str
    min: Mapping[str, int]
    max: Mapping[str, int]
    max_ratio: Mapping[str, Fraction]


@dataclass(frozen=True)
class LimitRange:
    """A namespace's LimitRange."""

    namespace: str
    limits: Tuple[LimitItem, ...]


# The API leaves an empty map or list out of an object, so one that is missing is empty.


def _texts(record: Optional[Record], key: str) -> Dict[str, str]:
    """Read an object of texts, such as labels."""
    found = None if record is None else record.optional_text_map(key)
    return {} if found is None else found


def _records(record: Optional[Record], key: str) -> List[Record]:
    """Read a list of objects."""
    found = None if record is None else record.optional_records(key)
    return [] if found is None else found


def _strings(record: Record, key: str) -> Tuple[str, ...]:
    """Read a list of texts."""
    found = record.optional_texts(key)
    return () if found is None else tuple(found)


def _resources(record: Optional[Record], key: str) -> Dict[str, int]:
    """Read a resource list, such as requests, in scheduler units."""
    return resources(_texts(record, key))


def read_node(record: Record) -> Node:
    """Read one node, as slim kept it."""
    metadata, spec, status = record.record("metadata"), record.optional_record("spec"), record.record("status")
    return Node(
        name=metadata.text("name"),
        labels=_texts(metadata, "labels"),
        taints=tuple(
            Taint(item.text("key"), item.optional_text("value"), item.text("effect"))
            for item in _records(spec, "taints")
        ),
        provider_id=None if spec is None else spec.optional_text("providerID"),
        capacity=resources(status.text_map("capacity")),
        allocatable=resources(status.text_map("allocatable")),
        kubelet_version=status.record("nodeInfo").text("kubeletVersion"),
    )


def _requirement(record: Record) -> Requirement:
    """Read one requirement of a selector."""
    return Requirement(record.text("key"), record.text("operator"), _strings(record, "values"))


def _selector(record: Optional[Record]) -> Optional[LabelSelector]:
    """Read a label selector."""
    if record is None:
        return None
    expressions = tuple(_requirement(item) for item in _records(record, "matchExpressions"))
    return LabelSelector(_texts(record, "matchLabels"), expressions)


def _node_terms(record: Optional[Record]) -> Optional[Tuple[NodeSelectorTerm, ...]]:
    """Read the terms of a required node selector."""
    if record is None:
        return None
    return tuple(
        NodeSelectorTerm(
            tuple(_requirement(item) for item in _records(term, "matchExpressions")),
            tuple(_requirement(item) for item in _records(term, "matchFields")),
        )
        for term in record.records("nodeSelectorTerms")
    )


def _affinity_terms(affinity: Optional[Record], kind: str) -> Tuple[AffinityTerm, ...]:
    """Read the required terms of a pod's podAffinity or podAntiAffinity."""
    group = None if affinity is None else affinity.optional_record(kind)
    return tuple(
        AffinityTerm(
            label_selector=_selector(term.optional_record("labelSelector")),
            namespaces=_strings(term, "namespaces"),
            namespace_selector=_selector(term.optional_record("namespaceSelector")),
            topology_key=term.text("topologyKey"),
            match_label_keys=_strings(term, "matchLabelKeys"),
            mismatch_label_keys=_strings(term, "mismatchLabelKeys"),
        )
        for term in _records(group, REQUIRED)
    )


def _spread(record: Record) -> SpreadConstraint:
    """Read one topology spread constraint."""
    return SpreadConstraint(
        max_skew=record.integer("maxSkew"),
        topology_key=record.text("topologyKey"),
        when_unsatisfiable=record.text("whenUnsatisfiable"),
        label_selector=_selector(record.optional_record("labelSelector")),
        min_domains=record.optional_integer("minDomains"),
        node_affinity_policy=record.optional_text("nodeAffinityPolicy"),
        node_taints_policy=record.optional_text("nodeTaintsPolicy"),
        match_label_keys=_strings(record, "matchLabelKeys"),
    )


def _container(record: Record) -> Container:
    """Read one container: its requests as stored, never taken from its limits."""
    lists = record.optional_record("resources")
    return Container(
        name=record.text("name"),
        requests=_resources(lists, "requests"),
        limits=_resources(lists, "limits"),
        host_ports=tuple(
            HostPort(port.integer("hostPort"), port.text("protocol")) for port in _records(record, "ports")
        ),
    )


def _init_container(record: Record) -> InitContainer:
    """Read one init container."""
    lists = record.optional_record("resources")
    return InitContainer(
        name=record.text("name"),
        requests=_resources(lists, "requests"),
        limits=_resources(lists, "limits"),
        sidecar=record.optional_text("restartPolicy") == SIDECAR_POLICY,
    )


def _controller(record: Record) -> Optional[OwnerRef]:
    """Read an object's controller, the only owner reference slim keeps."""
    owners = _records(record, "ownerReferences")
    return OwnerRef(owners[0].text("kind"), owners[0].text("name")) if owners else None


def read_pod(record: Record) -> Pod:
    """Read one pod, as slim kept it."""
    metadata, spec = record.record("metadata"), record.record("spec")
    affinity = spec.optional_record("affinity")
    node_affinity = None if affinity is None else affinity.optional_record("nodeAffinity")
    pod_lists = spec.optional_record("resources")
    return Pod(
        namespace=metadata.text("namespace"),
        name=metadata.text("name"),
        controller=_controller(metadata),
        node=spec.optional_text("nodeName"),
        labels=_texts(metadata, "labels"),
        overhead=_resources(spec, "overhead"),
        requests=None if pod_lists is None else _resources(pod_lists, "requests"),
        limits=None if pod_lists is None else _resources(pod_lists, "limits"),
        containers=tuple(_container(item) for item in spec.records("containers")),
        init_containers=tuple(_init_container(item) for item in _records(spec, "initContainers")),
        node_selector=_texts(spec, "nodeSelector"),
        node_affinity=_node_terms(None if node_affinity is None else node_affinity.optional_record(REQUIRED)),
        affinity=_affinity_terms(affinity, "podAffinity"),
        anti_affinity=_affinity_terms(affinity, "podAntiAffinity"),
        tolerations=tuple(
            Toleration(
                item.optional_text("key"),
                item.optional_text("operator"),
                item.optional_text("value"),
                item.optional_text("effect"),
            )
            for item in _records(spec, "tolerations")
        ),
        spread=tuple(_spread(item) for item in _records(spec, "topologySpreadConstraints")),
        claims=tuple(item.record("persistentVolumeClaim").text("claimName") for item in _records(spec, "volumes")),
    )


def read_owned(record: Record, kind: str) -> Owned:
    """Read one ReplicaSet or Job, as slim kept it; its kind's test kept only those with a controller."""
    metadata = record.record("metadata")
    owner = metadata.records("ownerReferences")[0]
    return Owned(
        kind, metadata.text("namespace"), metadata.text("name"), OwnerRef(owner.text("kind"), owner.text("name"))
    )


def read_claim(record: Record) -> PersistentVolumeClaim:
    """Read one claim."""
    metadata, spec = record.record("metadata"), record.optional_record("spec")
    return PersistentVolumeClaim(
        metadata.text("namespace"), metadata.text("name"), None if spec is None else spec.optional_text("volumeName")
    )


def read_volume(record: Record) -> PersistentVolume:
    """Read one volume, as slim kept it: its attributes hold only the tier keys."""
    metadata, spec = record.record("metadata"), record.record("spec")
    csi = spec.optional_record("csi")
    in_tree = (driver for key, driver in IN_TREE_DRIVERS if spec.optional_record(key) is not None)
    tiers = set(_texts(csi, "volumeAttributes").values())
    affinity = spec.optional_record("nodeAffinity")
    return PersistentVolume(
        name=metadata.text("name"),
        driver=next(in_tree, None) if csi is None else csi.text("driver"),
        # Two different tiers under the two keys name no one tier.
        tier=tiers.pop() if len(tiers) == 1 else None,
        node_affinity=_node_terms(None if affinity is None else affinity.optional_record("required")),
    )


def read_csi_node(record: Record) -> CsiNode:
    """Read one CSINode."""
    drivers = _records(record.optional_record("spec"), "drivers")
    return CsiNode(
        record.record("metadata").text("name"),
        {driver.text("name"): _count(driver.optional_record("allocatable")) for driver in drivers},
    )


def _count(allocatable: Optional[Record]) -> Optional[int]:
    """Read how many volumes a driver can attach on a node; None when it gives no limit."""
    return None if allocatable is None else allocatable.optional_integer("count")


def read_limit_range(record: Record) -> LimitRange:
    """Read one LimitRange."""
    limits = record.record("spec").records("limits")
    return LimitRange(
        record.record("metadata").text("namespace"),
        tuple(
            LimitItem(
                type=item.text("type"),
                min=_resources(item, "min"),
                max=_resources(item, "max"),
                max_ratio={name: quantity(text) for name, text in _texts(item, "maxLimitRequestRatio").items()},
            )
            for item in limits
        ),
    )


# ---------------------------------------------------------------- the pod labels placement uses


def selector_keys(selector: Optional[LabelSelector]) -> Set[str]:
    """Name the label keys a selector reads."""
    if selector is None:
        return set()
    return {*selector.match_labels, *(requirement.key for requirement in selector.match_expressions)}


def placement_label_keys(pods: Sequence[Pod]) -> FrozenSet[str]:
    """Name the label keys a required placement term of any pod uses, plus the add-on manager's mode.

    Those are the keys of the label selectors and matchLabelKeys of DoNotSchedule spread constraints, and of required
    pod affinity and anti-affinity terms, with their mismatchLabelKeys.
    """
    keys: Set[str] = {ADDON_MODE}
    for pod in pods:
        for constraint in pod.spread:
            if constraint.when_unsatisfiable == DO_NOT_SCHEDULE:
                keys.update(selector_keys(constraint.label_selector), constraint.match_label_keys)
        for term in (*pod.affinity, *pod.anti_affinity):
            keys.update(selector_keys(term.label_selector), term.match_label_keys, term.mismatch_label_keys)
    return frozenset(keys)


def with_placement_labels(pods: Sequence[Pod]) -> Tuple[Pod, ...]:
    """Keep of each pod's labels only those whose keys placement_label_keys names for these pods."""
    keys = placement_label_keys(pods)
    return tuple(replace(pod, labels={key: value for key, value in pod.labels.items() if key in keys}) for pod in pods)
