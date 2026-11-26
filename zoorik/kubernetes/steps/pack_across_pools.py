"""Step 4, packing across pools: every pod that may move is packed again across all the cluster's priced pools.

Each pool keeps the machine types step 3 left it. Besides its own machines, a pool may take new machines of each of its
lines in each zone where it has that line, one for each pod that fits one: each has the labels and taints that the
line's machines in that zone all share, and a name of its own. A pool on spot today takes pods only into room on its
own machines, so it gains no machine. A pool with a line that has no price takes no part: its pods stay, and its
machines take no pod.

Machines open cheapest per share of the moving pods' demand first, a pool's own before new ones of the same rank. A pod
that fits no empty machine stays on its node, and so does the node. A packing that needs more machines than those nodes
and one for each moving pod is not applied, and neither is one that doesn't make the cluster cost less. An AKS system
pool keeps at least one machine; any other pool may reach none.
"""

from dataclasses import replace
from fractions import Fraction
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from zoorik.kubernetes.model import ClusterState, Machine, Pod, Pool
from zoorik.kubernetes.packing import Job, pack
from zoorik.kubernetes.placement import HOSTNAME, Overhead, Placed, demand_of, empty_room, fits_machine, overheads
from zoorik.kubernetes.steps import SHARED_ROW, cost, minimum
from zoorik.kubernetes.steps.spare_machines import fits_empty, per_share
from zoorik.pricing.machines import Capacity

# The first part of a new machine's name, which no node's name has, since a node's name holds no slash.
ADDED = "added"

# A pool's line in one zone: the pool, the machine type, how the machines are bought, and the zone.
Group = Tuple[str, Optional[str], Optional[Capacity], str]


class PackAcrossPools:
    """Packs every pod that may move across all the cluster's priced pools, when that makes the cluster cost less."""

    name = "Packing across pools"
    row = SHARED_ROW

    def apply(self, state: ClusterState) -> ClusterState:
        """Pack the moving pods across the priced pools; the state as it was when that saves nothing."""
        packed = across(state)
        return packed if packed is not None and cost(packed) < cost(state) else state


def across(state: ClusterState) -> Optional[ClusterState]:
    """Give the state with the moving pods packed across the priced pools; None when they need more than the cap."""
    pools = [pool for pool in state.pools if pool.priced]
    ids = {pool.id for pool in pools}
    machines = [machine for machine in state.machines if machine.pool in ids]
    overhead = overheads(state)
    names = {machine.name for machine in machines}
    pods = [pod for pod in state.pods if pod.node in names and not pod.overhead]
    staying = {pod.key for pod in pods if pod.stays is not None or not fits_empty(pod, machines, overhead)}
    moving = tuple(pod for pod in pods if pod.key not in staying)
    kept = {pod.node for pod in pods if pod.key in staying}
    opened = tuple(machine for machine in machines if machine.name in kept)
    own = [machine for machine in machines if machine.name not in kept]
    candidates = ranked(pools, own, new_machines(machines, moving, overhead), moving, overhead)
    by_name = {machine.name: machine for machine in state.machines}
    others = tuple(machine for machine in state.machines if machine.pool not in ids)
    job = Job(
        pods=moving,
        fixed=tuple(
            Placed(pod, by_name[pod.node])
            for pod in state.pods
            if not pod.overhead and pod.node in by_name and (pod.node not in names or pod.key in staying)
        ),
        opened=opened,
        candidates=candidates,
        others=others,
        overheads=overhead,
        cap=len(opened) + len(moving),
    )
    packing = pack(job)
    if packing is None:
        return None
    return moved(state, [*others, *with_minimum(packing.machines, pools, candidates)], packing.where)


def new_machines(machines: Sequence[Machine], pods: Sequence[Pod], overhead: Mapping[str, Overhead]) -> List[Machine]:
    """Make the pools' new machines: for each line not on spot, in each zone it is in, one for each pod that fits."""
    groups: Dict[Group, List[Machine]] = {}
    for machine in machines:
        # A pool on spot today takes pods only into room on its own machines, so a line on spot gains no machine.
        if machine.capacity != Capacity.SPOT:
            groups.setdefault((machine.pool, machine.machine_type, machine.capacity, machine.zone), []).append(machine)
    made: List[Machine] = []
    for (pool, machine_type, _, zone), group in groups.items():
        model = shared(group, f"{ADDED}/{pool}/{machine_type}/{zone}")
        room = empty_room(model, overhead[pool])
        fitting = sum(1 for pod in pods if fits_machine(pod, model, room))
        made.extend(named(model, f"{model.name}/{index}") for index in range(fitting))
    return made


def shared(machines: Sequence[Machine], name: str) -> Machine:
    """Give a machine called name with what machines all share: their labels and taints, and the first one's room."""
    first = machines[0]
    labels = {
        key: value
        for key, value in first.labels.items()
        if key != HOSTNAME and all(machine.labels.get(key) == value for machine in machines)
    }
    taints = tuple(taint for taint in first.taints if all(taint in machine.taints for machine in machines))
    return named(replace(first, labels=labels, taints=taints), name)


def named(machine: Machine, name: str) -> Machine:
    """Give the machine under another name, its hostname label with it."""
    return replace(machine, name=name, labels={**machine.labels, HOSTNAME: name})


def ranked(
    pools: Sequence[Pool],
    own: Sequence[Machine],
    new: Sequence[Machine],
    pods: Sequence[Pod],
    overhead: Mapping[str, Overhead],
) -> Tuple[Machine, ...]:
    """Order the pools' own machines and new ones cheapest first per share of the pods' demand, own first in a tie."""
    hourly = {
        (pool.id, line.machine_type, line.capacity): line.hourly
        for pool in pools
        for line in pool.lines
        if line.hourly is not None
    }
    demand = demand_of(pods)

    def key(item: Tuple[bool, Machine]) -> Tuple[int, Fraction, bool, str]:
        added, machine = item
        price = hourly[(machine.pool, machine.machine_type, machine.capacity)]
        last, share, name = per_share(machine, price, empty_room(machine, overhead[machine.pool]).free, demand)
        return (last, share, added, name)

    items = [*((False, machine) for machine in own), *((True, machine) for machine in new)]
    return tuple(machine for _, machine in sorted(items, key=key))


def with_minimum(machines: Sequence[Machine], pools: Sequence[Pool], candidates: Sequence[Machine]) -> List[Machine]:
    """Give machines with each pool at the platform's minimum: a pool below it keeps its first candidates."""
    left = list(machines)
    for pool in pools:
        short = minimum(pool) - sum(1 for machine in left if machine.pool == pool.id)
        left.extend([machine for machine in candidates if machine.pool == pool.id][: max(0, short)])
    return left


def moved(state: ClusterState, machines: Sequence[Machine], where: Mapping[str, str]) -> ClusterState:
    """Give the state with machines, and each pod the packing placed on its machine, in that machine's pool."""
    by_name = {machine.name: machine for machine in machines}
    return replace(
        state,
        machines=tuple(sorted(machines, key=lambda machine: machine.name)),
        pods=tuple(
            replace(pod, node=where[pod.key], pool=by_name[where[pod.key]].pool) if pod.key in where else pod
            for pod in state.pods
        ),
    )
