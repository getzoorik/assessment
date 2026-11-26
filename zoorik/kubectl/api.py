"""One cluster's API, read whole: the protocol both read methods give, and the snapshot they give it as."""

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Protocol, Sequence, Tuple, Union

from zoorik.cloud.fields import Record
from zoorik.kubectl.objects import (
    AUTOSCALERS,
    BUDGETS,
    CLAIMS,
    CSI_NODES,
    JOBS,
    LIMIT_RANGES,
    NODES,
    PODS,
    REPLICASETS,
    SCALING_KINDS,
    VOLUMES,
    Autoscaler,
    CsiNode,
    DisruptionBudget,
    Json,
    Kind,
    LimitRange,
    Node,
    Owned,
    PersistentVolume,
    PersistentVolumeClaim,
    Pod,
    read_autoscaler,
    read_budget,
    read_claim,
    read_csi_node,
    read_limit_range,
    read_node,
    read_owned,
    read_pod,
    read_volume,
    with_kept_labels,
)
from zoorik.kubectl.usage import SampleWindow, Usage

# Both read methods list each kind in pages of 500, and give each request to the API server 60 s.
PAGE_SIZE = 500
REQUEST_TIMEOUT = "60s"


@dataclass(frozen=True)
class Scaling:
    """A cluster's HorizontalPodAutoscalers and PodDisruptionBudgets, and why one of the two couldn't be read."""

    autoscalers: Tuple[Autoscaler, ...]
    budgets: Tuple[DisruptionBudget, ...]
    # None when both were read.
    reason: Optional[str]


@dataclass(frozen=True)
class Snapshot:
    """A cluster as the run read it: every kind the allowlist names, and its usage over the window."""

    nodes: Tuple[Node, ...]
    pods: Tuple[Pod, ...]
    owners: Tuple[Owned, ...]
    claims: Tuple[PersistentVolumeClaim, ...]
    volumes: Tuple[PersistentVolume, ...]
    csi_nodes: Tuple[CsiNode, ...]
    limit_ranges: Tuple[LimitRange, ...]
    scaling: Scaling
    usage: Usage


class KubeApi(Protocol):
    """A cluster's API, read whole by one read method."""

    def read(self, window: SampleWindow) -> Snapshot:
        """Read every kind and sample usage over window; a kind that can't be read makes the cluster unreadable."""


def snapshot(
    items: Mapping[str, Sequence[Json]], scaling: Mapping[str, Union[Sequence[Json], str]], usage: Usage
) -> Snapshot:
    """Make the snapshot from each kind's kept items, by kind name, and the usage.

    Each scaling kind gives its kept items, or the reason it couldn't be read; one that couldn't be read has no items.
    """
    found: Dict[str, Sequence[Json]] = {
        **items,
        **{name: [] if isinstance(value, str) else value for name, value in scaling.items()},
    }

    def records(kind: Kind) -> List[Record]:
        """Give kind's items as records, each named by its place in the list."""
        return [Record(item, f"{kind.resource}[{index}]") for index, item in enumerate(found[kind.name])]

    budgets = tuple(read_budget(record) for record in records(BUDGETS))
    reasons = [value for value in (scaling[kind.name] for kind in SCALING_KINDS) if isinstance(value, str)]
    return Snapshot(
        nodes=tuple(read_node(record) for record in records(NODES)),
        pods=with_kept_labels([read_pod(record) for record in records(PODS)], budgets),
        owners=tuple(read_owned(record, kind.item_kind) for kind in (REPLICASETS, JOBS) for record in records(kind)),
        claims=tuple(read_claim(record) for record in records(CLAIMS)),
        volumes=tuple(read_volume(record) for record in records(VOLUMES)),
        csi_nodes=tuple(read_csi_node(record) for record in records(CSI_NODES)),
        limit_ranges=tuple(read_limit_range(record) for record in records(LIMIT_RANGES)),
        scaling=Scaling(
            tuple(read_autoscaler(record) for record in records(AUTOSCALERS)), budgets, next(iter(reasons), None)
        ),
        usage=usage,
    )
