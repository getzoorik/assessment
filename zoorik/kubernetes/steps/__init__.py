"""The saving steps, in order, each on what the step before left, and the cost rules they are measured by.

A line costs its machines' hourly price for 730 hours, rounded to whole dollars; a state costs the sum of its priced
lines. A step's saving is the cost before it less the cost after it, and a step that would cost more is not applied,
so no step is negative and the steps add up exactly to the cost today less the cost after.
"""

from typing import Protocol, Sequence, Tuple

from zoorik.format import half_up
from zoorik.kubernetes.model import ClusterState, Pool, StepResult
from zoorik.pricing.machines import HOURS_PER_MONTH

# The page-1 row the smaller-pods and spare-machines steps count in, and whose saving the workload table splits.
SHARED_ROW = "smaller pods and spare machines"


class Step(Protocol):
    """A saving step: its name, the page-1 row it counts in, and the state it leaves."""

    @property
    def name(self) -> str:
        """Name the step."""

    @property
    def row(self) -> str:
        """Name the page-1 row the step's saving counts in."""

    def apply(self, state: ClusterState) -> ClusterState:
        """Give the state the step leaves."""


def line_cost(count: int, hourly: float) -> int:
    """Give a line's cost a month in whole dollars: its machines at their hourly price for 730 hours."""
    return half_up(count * hourly * HOURS_PER_MONTH)


def cost(state: ClusterState) -> int:
    """Give a state's cost a month: the sum of its priced lines; an unpriced line counts in no total."""
    counts = state.counts()
    return sum(
        line_cost(counts.get((pool.id, line.machine_type, line.capacity), 0), line.hourly)
        for pool in state.pools
        for line in pool.lines
        if line.hourly is not None
    )


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
            results.append(StepResult(step.name, step.row, before, after))
            state = after_state
        return state, tuple(results)
