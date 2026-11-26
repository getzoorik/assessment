"""Packing: pods onto as few machines of a set as hold them, largest first, in two passes.

The first pass places each pod on the first opened machine where the checker passes, and opens one more machine when
none does, taking the zones in turn. The second pass places every pod again against that fixed set, so spread and
anti-affinity count every machine of the set, empty ones included; a pod that no longer fits opens one more machine,
and the pass runs again.
"""

from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from zoorik.kubernetes.model import Machine, Pod, Resources
from zoorik.kubernetes.placement import Board, Overhead, Placed, Rules, empty_room, largest, term_of


@dataclass(frozen=True)
class Job:
    """What one packing places, and the machines and pods it works among."""

    pods: Tuple[Pod, ...]
    # Pods that stay where they are: those of the other pools, and those that keep their node.
    fixed: Tuple[Placed, ...]
    # The machines in the set from the start: those that keep a pod on its node.
    opened: Tuple[Machine, ...]
    # The machines that may open, cheapest per share of the pods' demand first.
    candidates: Tuple[Machine, ...]
    # The other pools' machines, which stay as they are and count in spread.
    others: Tuple[Machine, ...]
    overheads: Mapping[str, Overhead]
    # The most machines the set may hold; a packing that needs more stops there.
    cap: int


@dataclass(frozen=True)
class Packing:
    """A packing that holds every pod: the machines of the set, and each pod's machine by pod key."""

    machines: Tuple[Machine, ...]
    where: Mapping[str, str]


def largest_share(request: Resources, room: Resources) -> Fraction:
    """Give a request's largest share of a machine's room, over the resources the room has."""
    return max(
        (Fraction(amount, room[key]) for key, amount in request.items() if room.get(key, 0) > 0), default=Fraction(0)
    )


def order(pods: Sequence[Pod], room: Resources) -> List[Pod]:
    """Order pods for packing: largest share of room first, then by name; a pod with affinity after its targets.

    A pod's targets are the other pods that match all of its affinity terms. Pods whose targets wait on each other go
    in size order.
    """
    by_size = sorted(pods, key=lambda pod: (-largest_share(pod.request, room), pod.key))
    targets: Dict[str, Set[str]] = {}
    for pod in by_size:
        if pod.source.affinity:
            terms = [term_of(item, pod.source) for item in pod.source.affinity]
            targets[pod.key] = {
                other.key for other in by_size if other is not pod and all(term.covers(other.source) for term in terms)
            }
    done: Set[str] = set()
    waiting = list(by_size)
    ordered: List[Pod] = []
    while waiting:
        ready = next((index for index, pod in enumerate(waiting) if targets.get(pod.key, set()) <= done), 0)
        pod = waiting.pop(ready)
        ordered.append(pod)
        done.add(pod.key)
    return ordered


class Opener:
    """Opens candidate machines zone by zone: each opening starts at the zone after the one the last opened in."""

    def __init__(self, candidates: Sequence[Machine]) -> None:
        """Open from candidates, in their order within each zone."""
        self._waiting = list(candidates)
        self._zones = sorted({machine.zone for machine in candidates})
        self._next = 0

    def open(self, fits: Callable[[Machine], bool]) -> Optional[Machine]:
        """Open the first waiting machine that fits, from the next zone on; None when no waiting machine fits."""
        for step in range(len(self._zones)):
            index = (self._next + step) % len(self._zones)
            zone = self._zones[index]
            found = next((machine for machine in self._waiting if machine.zone == zone and fits(machine)), None)
            if found is not None:
                self._waiting.remove(found)
                self._next = index + 1
                return found
        return None


def pack(job: Job) -> Optional[Packing]:
    """Pack the job's pods onto as few machines as hold them; None when they need more than the cap or fit nowhere."""
    machines = (*job.opened, *job.candidates)
    room: Dict[str, int] = {}
    for machine in machines:
        room = largest(room, empty_room(machine, job.overheads[machine.pool]).free)
    pods = order(job.pods, room)
    opener = Opener(job.candidates)
    opened = list(job.opened)
    board = _board(job, (*job.others, *machines), opened)
    for pod in pods:
        if _first_fit(board, pod, opened) is None and not _open(job, board, pod, opened, opener):
            return None
    while True:
        board = _board(job, (*job.others, *opened), opened)
        failed = next((pod for pod in pods if _first_fit(board, pod, opened) is None), None)
        if failed is None:
            return Packing(tuple(opened), {pod.key: board.where[pod.key] for pod in pods})
        if not _open(job, board, failed, opened, opener):
            return None


def _board(job: Job, universe: Sequence[Machine], opened: Sequence[Machine]) -> Board:
    """Make a pass's board: universe counted, opened open, and the fixed pods placed."""
    board = Board(universe, job.overheads)
    for machine in opened:
        board.open(machine)
    for placed in job.fixed:
        board.place(placed.pod, placed.machine)
    return board


def _first_fit(board: Board, pod: Pod, opened: Sequence[Machine]) -> Optional[Machine]:
    """Place pod on the first opened machine it fits, and give that machine; None when it fits none."""
    rules = board.rules(pod)
    machine = next((machine for machine in opened if board.fits(pod, machine, rules)), None)
    if machine is not None:
        board.place(pod, machine)
    return machine


def _open(job: Job, board: Board, pod: Pod, opened: List[Machine], opener: Opener) -> bool:
    """Open one more machine that pod fits, within the cap, and place pod there; False when there is none."""
    if len(opened) >= job.cap:
        return False
    rules: Rules = board.rules(pod)
    machine = opener.open(lambda candidate: board.fits(pod, candidate, rules))
    if machine is None:
        return False
    opened.append(machine)
    board.open(machine)
    board.place(pod, machine)
    return True
