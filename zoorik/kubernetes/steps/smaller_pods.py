"""Step 1, smaller pods: each container's request becomes its workload's peak use x 1.3, with today's floors.

One rule both ways: a container that uses more than it asks for is raised, so the machines hold what the pods really
use. Every replica of a workload gets one request per container, which holds its busiest replica. A request stays
within the container's limit and the namespace's LimitRanges. A workload whose raise a machine has no room for keeps
today's requests, and the report names it as held. The machines don't change, so the step alone saves $0.
"""

import math
from dataclasses import replace
from fractions import Fraction
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from zoorik.kubectl.objects import ADDON_MODE, LimitItem, LimitRange
from zoorik.kubectl.objects import Pod as ApiPod
from zoorik.kubectl.quantity import CPU
from zoorik.kubectl.usage import MEBIBYTE, ContainerKey, Peak
from zoorik.kubernetes.model import MEMORY, ClusterState, Machine, Pod, Resources, Workload
from zoorik.kubernetes.placement import (
    added,
    every_container,
    largest,
    limits_of,
    overheads,
    pod_request,
    pod_total,
    requests_of,
)
from zoorik.kubernetes.steps import SHARED_ROW

HEADROOM = Fraction(13, 10)
# Today's floors: 0.010 cores, in millicores, and 32 MiB, in bytes.
CPU_FLOOR = 10
MEMORY_FLOOR = 32 * MEBIBYTE
FLOORS = {CPU: CPU_FLOOR, MEMORY: MEMORY_FLOOR}
# The resources the step sizes; every other resource keeps today's request.
SIZED = (CPU, MEMORY)
# The platform puts back a pod it manages in this mode, so the pod keeps its requests.
RECONCILE = "Reconcile"
CONTAINER_LIMITS = "Container"
POD_LIMITS = "Pod"

# A request for each running container with a reading, by container name.
Sizes = Dict[str, Dict[str, int]]


def sized(peak: Peak) -> Dict[str, int]:
    """Give the request for a peak: the peak x 1.3, rounded up, and never below the floors."""
    use = {CPU: peak.cpu, MEMORY: peak.memory}
    return {key: max(math.ceil(use[key] * HEADROOM), FLOORS[key]) for key in SIZED}


def bounded(size: int, limit: Optional[int], items: Sequence[LimitItem], resource: str) -> int:
    """Keep a request within its limit and the LimitRanges' min, max and limit-to-request ratio."""
    low = [item.min[resource] for item in items if resource in item.min]
    high = [item.max[resource] for item in items if resource in item.max]
    if limit is not None:
        high.append(limit)
        low += [math.ceil(Fraction(limit) / item.max_ratio[resource]) for item in items if resource in item.max_ratio]
    return min([max([size, *low]), *high])


def running(pod: ApiPod) -> Tuple[str, ...]:
    """Name the pod's containers that run side by side: its containers and its sidecars."""
    sidecars = (init.name for init in pod.init_containers if init.sidecar)
    return (*(container.name for container in pod.containers), *sidecars)


def container_requests(pod: ApiPod, sizes: Sizes, items: Sequence[LimitItem]) -> Dict[str, Resources]:
    """Give each container's requests by name: a running container with a size gets it, within its bounds.

    Other init containers, and containers with no reading, keep today's requests.
    """
    bounds = [item for item in items if item.type == CONTAINER_LIMITS]
    requests = requests_of(pod)
    for container in every_container(pod):
        size = sizes.get(container.name)
        if size is not None:
            new = {key: bounded(value, container.limits.get(key), bounds, key) for key, value in size.items()}
            requests[container.name] = {**container.requests, **new}
    return requests


def pod_level_requests(pod: ApiPod, total: Optional[Dict[str, int]]) -> Optional[Resources]:
    """Give the pod-level requests: those the pod sets follow its summed peaks, within its pod-level limits."""
    if pod.requests is None or total is None:
        return pod.requests
    limits = {} if pod.limits is None else pod.limits
    new = {key: bounded(value, limits.get(key), (), key) for key, value in total.items() if key in pod.requests}
    return {**pod.requests, **new}


def breaks_pod_limits(total: Resources, limits: Resources, items: Sequence[LimitItem]) -> bool:
    """Tell whether a pod's total requests break a LimitRange of type Pod: its min, its max or its ratio."""
    for item in items:
        if any(total.get(key, 0) < least for key, least in item.min.items()):
            return True
        if any(total.get(key, 0) > most for key, most in item.max.items()):
            return True
        for key, ratio in item.max_ratio.items():
            # The API server refuses a ratio over a request of 0; a pod with no limit has no ratio to keep.
            if limits.get(key, 0) > 0 and (total.get(key, 0) == 0 or Fraction(limits[key], total[key]) > ratio):
                return True
    return False


def was_raised(pod: ApiPod, requests: Mapping[str, Resources], pod_level: Optional[Resources]) -> bool:
    """Tell whether any container's or the pod-level CPU or memory request went up."""
    pairs = [(old, requests[name]) for name, old in requests_of(pod).items()]
    if pod.requests is not None and pod_level is not None:
        pairs.append((pod.requests, pod_level))
    return any(new.get(key, 0) > old.get(key, 0) for old, new in pairs for key in SIZED)


def loads(state: ClusterState) -> Dict[str, Dict[str, int]]:
    """Give what each machine holds as the steps count it: its pool's overhead and the requests of its other pods."""
    overhead = overheads(state)
    load = {machine.name: dict(overhead[machine.pool].requests) for machine in state.machines}
    for pod in state.pods:
        if not pod.overhead:
            load[pod.node] = added(load[pod.node], pod.request)
    return load


def no_room(machine: Machine, load: Resources, today: Resources) -> bool:
    """Tell whether a machine holds more of a resource than its allocatable, and more of it than today."""
    return any(amount > machine.allocatable.get(key, 0) and amount > today.get(key, 0) for key, amount in load.items())


def held_workloads(today: ClusterState, resized: Sequence[Pod]) -> Set[Workload]:
    """Give the workloads whose raise a machine has no room for, which keep today's requests.

    A raised pod on such a machine, or a raised DaemonSet pod of its pool, holds its workload back. A held workload's
    pods all go back to today's requests, which can leave another machine short, so the check runs until none is.
    """
    held: Set[Workload] = set()
    before = loads(today)
    while True:
        pods = tuple(old if old.workload in held else new for old, new in zip(today.pods, resized))
        after = loads(replace(today, pods=pods))
        full = [machine for machine in today.machines if no_room(machine, after[machine.name], before[machine.name])]
        names = {machine.name for machine in full}
        pools = {machine.pool for machine in full}
        holding = {
            pod.workload for pod in pods if pod.raised and (pod.pool in pools if pod.overhead else pod.node in names)
        }
        if not holding:
            return held
        held |= holding


class SmallerPods:
    """Gives each workload's containers one request each, from the peaks the run read."""

    name = "Smaller pods"
    row = SHARED_ROW
    assumption = ""

    def __init__(self, peaks: Mapping[ContainerKey, Peak], limit_ranges: Sequence[LimitRange]) -> None:
        """Size from peaks, within limit_ranges."""
        self._peaks = peaks
        self._ranges: Dict[str, List[LimitItem]] = {}
        for limit_range in limit_ranges:
            self._ranges.setdefault(limit_range.namespace, []).extend(limit_range.limits)

    def apply(self, state: ClusterState) -> ClusterState:
        """Give each pod its workload's requests; static pods, pods the platform puts back and held pods keep theirs."""
        sizes: Dict[Workload, Sizes] = {}
        totals: Dict[Workload, Dict[str, int]] = {}
        for pod in state.pods:
            readings = self._readings(pod.source)
            workload = sizes.setdefault(pod.workload, {})
            for name, size in readings.items():
                workload[name] = largest(workload.get(name, {}), size)
            if pod.source.requests is not None and len(readings) == len(running(pod.source)):
                totals[pod.workload] = largest(totals.get(pod.workload, {}), self._summed(pod.source))
        resized = [self._resized(pod, sizes[pod.workload], totals.get(pod.workload)) for pod in state.pods]
        held = held_workloads(state, resized)
        pods = tuple(replace(old, held=True) if old.workload in held else new for old, new in zip(state.pods, resized))
        return replace(state, pods=pods)

    def _readings(self, pod: ApiPod) -> Sizes:
        """Give the request each of the pod's running containers with a reading would get."""
        readings: Sizes = {}
        for name in running(pod):
            peak = self._peaks.get(ContainerKey(pod.namespace, pod.name, name))
            if peak is not None:
                readings[name] = sized(peak)
        return readings

    def _summed(self, pod: ApiPod) -> Dict[str, int]:
        """Give the pod-level request for the summed peaks of the pod's running containers, each with a reading."""
        peaks = [self._peaks[ContainerKey(pod.namespace, pod.name, name)] for name in running(pod)]
        return sized(Peak(sum(peak.cpu for peak in peaks), sum(peak.memory for peak in peaks)))

    def _resized(self, pod: Pod, sizes: Sizes, total: Optional[Dict[str, int]]) -> Pod:
        """Give the pod with its workload's requests, or as it is when it keeps today's."""
        source = pod.source
        if source.static or source.labels.get(ADDON_MODE) == RECONCILE:
            return pod
        items = self._ranges.get(source.namespace, [])
        requests = container_requests(source, sizes, items)
        pod_level = pod_level_requests(source, total)
        limits = pod_total(source, limits_of(source), source.limits)
        if breaks_pod_limits(
            pod_total(source, requests, pod_level), limits, [i for i in items if i.type == POD_LIMITS]
        ):
            return pod
        return replace(
            pod, request=pod_request(source, requests, pod_level), raised=was_raised(source, requests, pod_level)
        )
