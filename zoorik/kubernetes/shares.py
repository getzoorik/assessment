"""The workload table: one row's saving split by the CPU and memory each workload frees.

A workload's weight is half its share of the CPU all workloads free plus half its share of the memory, or its share of
the one resource freed when only one is. Each share is rounded down, and the dollars left go to the largest remainders,
so the shares add up to the saving exactly. A raised workload frees nothing, nor does a held one, which keeps today's
requests. Shares are never added on top.
"""

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, List, Mapping, Tuple

from zoorik.kubectl.quantity import CPU
from zoorik.kubernetes.model import MEMORY, ClusterState, Workload, WorkloadResult


@dataclass(frozen=True)
class Freed:
    """What a workload frees over its pods: CPU in millicores and memory in bytes."""

    cpu: int
    memory: int


def name_of(workload: Workload) -> Tuple[str, str, str]:
    """Give a workload's sort key: its namespace, its name and its kind."""
    return (workload.namespace, workload.name, workload.kind)


def split(saving: int, freed: Mapping[Workload, Freed]) -> Dict[Workload, int]:
    """Split saving between workloads by what each frees, so the shares add up to it exactly."""
    cpu = sum(each.cpu for each in freed.values())
    memory = sum(each.memory for each in freed.values())
    if cpu == 0 and memory == 0:
        return {workload: 0 for workload in freed}
    exact: Dict[Workload, Fraction] = {}
    for workload, each in freed.items():
        parts: List[Fraction] = []
        if cpu:
            parts.append(Fraction(each.cpu, cpu))
        if memory:
            parts.append(Fraction(each.memory, memory))
        exact[workload] = saving * sum(parts, Fraction(0)) / len(parts)
    shares = {workload: math.floor(value) for workload, value in exact.items()}
    left = saving - sum(shares.values())
    ranked = sorted(exact, key=lambda workload: (shares[workload] - exact[workload], name_of(workload)))
    for workload in ranked[:left]:
        shares[workload] += 1
    return shares


def workload_results(today: ClusterState, after: ClusterState, saving: int) -> Tuple[WorkloadResult, ...]:
    """Give each workload's freed CPU and memory from today's requests to after's, and its share of saving."""
    before = {pod.key: pod for pod in today.pods}
    freed: Dict[Workload, Freed] = {}
    raised = set()
    held = set()
    for pod in after.pods:
        old = before[pod.key]
        each = freed.get(pod.workload, Freed(0, 0))
        freed[pod.workload] = Freed(
            each.cpu + old.request.get(CPU, 0) - pod.request.get(CPU, 0),
            each.memory + old.request.get(MEMORY, 0) - pod.request.get(MEMORY, 0),
        )
        if pod.raised:
            raised.add(pod.workload)
        if pod.held:
            held.add(pod.workload)
    for workload in raised:
        freed[workload] = Freed(0, 0)
    shares = split(saving, freed)
    return tuple(
        WorkloadResult(
            workload.kind,
            workload.namespace,
            workload.name,
            freed[workload].cpu,
            freed[workload].memory,
            workload in raised,
            workload in held,
            shares[workload],
        )
        for workload in sorted(freed, key=name_of)
    )
