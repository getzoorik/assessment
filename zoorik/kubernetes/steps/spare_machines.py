"""Step 2, spare machines: each pool's pods are packed onto as few of today's machines as hold them.

A pool with an unpriced line takes no part. Pods that stay put keep their nodes, which stay in the set; a pod that fits
no empty machine of its pool stays too. A packing that needs as many machines as today is not applied. An AKS system
pool keeps at least one machine.
"""

from dataclasses import replace
from fractions import Fraction
from typing import Mapping, Optional, Sequence, Tuple

from zoorik.kubectl.quantity import CPU
from zoorik.kubernetes.model import MEMORY, ClusterState, Machine, Pod, Pool, Resources
from zoorik.kubernetes.packing import Job, pack
from zoorik.kubernetes.placement import Overhead, Placed, demand_of, empty_room, fits_machine, overheads
from zoorik.kubernetes.steps import SHARED_ROW, minimum

# Cheapest first: a machine's price per share of the pods' demand, or, for a machine with no room for that demand, last.
Rank = Tuple[int, Fraction, str]


def per_share(machine: Machine, hourly: float, room: Resources, demand: Resources) -> Rank:
    """Rank a machine by its hourly price over the smaller of its room's shares of the pods' CPU and memory demand."""
    shares = [Fraction(room.get(key, 0), demand[key]) for key in (CPU, MEMORY) if demand.get(key, 0) > 0]
    smallest = min(shares, default=Fraction(1))
    if smallest <= 0:
        return (1, Fraction(0), machine.name)
    return (0, Fraction(hourly) / smallest, machine.name)


class SpareMachines:
    """Packs each priced pool's pods onto as few of its machines as hold them."""

    name = "Spare machines"
    row = SHARED_ROW

    def apply(self, state: ClusterState) -> ClusterState:
        """Pack each priced pool in turn, by pool id."""
        for pool in sorted(state.pools, key=lambda each: each.id):
            if pool.priced:
                state = spare(state, pool)
        return state


def spare(state: ClusterState, pool: Pool) -> ClusterState:
    """Pack one pool's pods onto as few of its machines as hold them; the state as it was when that saves nothing."""
    machines = [machine for machine in state.machines if machine.pool == pool.id]
    by_name = {machine.name: machine for machine in state.machines}
    overhead = overheads(state)
    names = {machine.name for machine in machines}
    pods = [pod for pod in state.pods if pod.node in names and not pod.overhead]
    staying = {pod.key for pod in pods if pod.stays is not None or not fits_empty(pod, machines, overhead)}
    moving = tuple(pod for pod in pods if pod.key not in staying)
    kept = {pod.node for pod in pods if pod.key in staying}
    candidates = rank(pool, [machine for machine in machines if machine.name not in kept], moving, overhead[pool.id])
    fixed = tuple(
        Placed(pod, by_name[pod.node])
        for pod in state.pods
        if not pod.overhead and pod.node in by_name and (pod.node not in names or pod.key in staying)
    )
    job = Job(
        pods=moving,
        fixed=fixed,
        opened=tuple(machine for machine in machines if machine.name in kept),
        candidates=candidates,
        others=tuple(machine for machine in state.machines if machine.pool != pool.id),
        overheads=overhead,
        cap=len(machines) - 1,
    )
    packing = pack(job)
    if packing is None:
        return state
    left = [machine.name for machine in packing.machines]
    # A pool the packing empties keeps the platform's minimum, its cheapest machines first.
    left += [machine.name for machine in candidates if machine.name not in left][: max(0, minimum(pool) - len(left))]
    if len(left) >= len(machines):
        return state
    return replace(
        state,
        machines=tuple(machine for machine in state.machines if machine.pool != pool.id or machine.name in left),
        pods=tuple(moved(pod, packing.where) for pod in state.pods),
    )


def fits_empty(pod: Pod, machines: Sequence[Machine], overhead: Mapping[str, Overhead]) -> bool:
    """Tell whether the pod fits one of machines when empty, by the rules that need no other pod."""
    return any(fits_machine(pod, machine, empty_room(machine, overhead[machine.pool])) for machine in machines)


def rank(pool: Pool, machines: Sequence[Machine], pods: Sequence[Pod], overhead: Overhead) -> Tuple[Machine, ...]:
    """Order a pool's machines cheapest first per share of the pods' demand, then by name."""
    hourly = {(line.machine_type, line.capacity): line.hourly for line in pool.lines if line.hourly is not None}
    demand = demand_of(pods)

    def key(machine: Machine) -> Rank:
        room = empty_room(machine, overhead).free
        return per_share(machine, hourly[(machine.machine_type, machine.capacity)], room, demand)

    return tuple(sorted(machines, key=key))


def moved(pod: Pod, where: Mapping[str, str]) -> Pod:
    """Give the pod on the machine the packing put it on, or as it is."""
    node: Optional[str] = where.get(pod.key)
    return pod if node is None else replace(pod, node=node)
