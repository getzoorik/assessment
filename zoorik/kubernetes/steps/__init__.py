"""The saving steps, in order, each on what the step before left, and the cost rules they are measured by.

A line costs 730 hours a month, rounded to whole dollars: half of them at its machines' hourly price, and the other
half, the quiet hours, at its machines then, at the spot price when they run spot then. Until apps run fewer copies at
quiet times the two halves hold the same machines. A state costs the sum of its priced lines. A step's saving is the
cost before it less the cost after it, and a step that would cost more is not applied, so no step is negative and the
steps add up exactly to the cost today less the cost after.
"""

from typing import Optional, Protocol, Sequence, Tuple

from zoorik.format import half_up
from zoorik.kubernetes.model import ClusterState, Pool, StepResult
from zoorik.pricing.machines import HOURS_PER_MONTH

# The page-1 row smaller pods, spare machines and packing across pools count in, whose saving the workload table splits.
SHARED_ROW = "smaller pods and spare machines (packing included)"
# The quiet hours' share of the day: 12 hours.
QUIET_SHARE = 0.5


class Step(Protocol):
    """A saving step: its name, the page-1 row it counts in, the assumption it rests on, and the state it leaves."""

    @property
    def name(self) -> str:
        """Name the step."""

    @property
    def row(self) -> str:
        """Name the page-1 row the step's saving counts in."""

    @property
    def assumption(self) -> str:
        """State the assumption the step's figure rests on, for the report to show beside it; "" for none."""

    def apply(self, state: ClusterState) -> ClusterState:
        """Give the state the step leaves."""


def line_cost(busy: int, quiet: int, hourly: float, spot: Optional[float]) -> int:
    """Give a line's cost a month in whole dollars: its busy machines and its quiet ones, each for half of 730 hours.

    The busy machines are at the hourly price, and the quiet ones too unless they run spot then.
    """
    quiet_hourly = hourly if spot is None else spot
    return half_up(HOURS_PER_MONTH * ((1 - QUIET_SHARE) * busy * hourly + QUIET_SHARE * quiet * quiet_hourly))


def cost(state: ClusterState) -> int:
    """Give a state's cost a month: the sum of its priced lines; an unpriced line counts in no total."""
    busy, quiet = state.counts(), state.quiet_counts()
    total = 0
    for pool in state.pools:
        for line in pool.lines:
            if line.hourly is not None:
                key = (pool.id, line.machine_type, line.capacity)
                total += line_cost(busy.get(key, 0), quiet.get(key, 0), line.hourly, line.spot)
    return total


def minimum(pool: Pool) -> int:
    """Give the fewest machines the platform lets a pool have: an AKS system pool keeps 1, any other may reach 0."""
    return 1 if pool.system else 0


class Pipeline:
    """The steps in order: each works on what the one before left, and one that would cost more is not applied."""

    def __init__(self, steps: Sequence[Step]) -> None:
        """Run steps, in their order."""
        self._steps = tuple(steps)

    def run(self, state: ClusterState) -> Tuple[ClusterState, Tuple[StepResult, ...]]:
        """Run every step on state, and give the state the last one leaves and each step's costs."""
        results = []
        for step in self._steps:
            before = cost(state)
            after_state = step.apply(state)
            after = cost(after_state)
            if after > before:
                after_state, after = state, before
            results.append(StepResult(step.name, step.row, step.assumption, before, after))
            state = after_state
        return state, tuple(results)
