"""Step 6, spot: the machines that run at quiet times are counted on spot for those 12 hours.

Every cluster is counted the same way. Each line's machines at quiet times run at its machine type's spot price then,
and the machines of the other 12 hours stay at the regular price. An AKS system pool stays on regular machines, a line
already on spot stays as it is, and a line whose type has no spot price stays at the regular price, with the reason.
"""

from dataclasses import replace
from typing import Mapping, Union

from zoorik.kubernetes.model import ClusterState, Line, Pool
from zoorik.pricing.machines import Capacity

ROW = "spot"
ASSUMPTION = "the machines running at quiet times, 12 h a day, on spot"


class Spot:
    """Prices each line that can run spot at its spot price for its machines at quiet times."""

    name = "Spot"
    row = ROW
    assumption = ASSUMPTION

    def __init__(self, prices: Mapping[str, Union[float, str]]) -> None:
        """Count spot at prices: each machine type's hourly spot price in the cluster's region, or why it has none."""
        self._prices = prices

    def apply(self, state: ClusterState) -> ClusterState:
        """Give the state with each line that can run spot priced at spot for its quiet hours."""
        pools = tuple(replace(pool, lines=tuple(self._line(pool, line) for line in pool.lines)) for pool in state.pools)
        return replace(state, pools=pools)

    def _line(self, pool: Pool, line: Line) -> Line:
        """Give a line at spot for its quiet hours, or with the reason it has no spot price, or as it was."""
        if pool.system or line.capacity is not Capacity.REGULAR or line.hourly is None or line.machine_type is None:
            return line
        price = self._prices[line.machine_type]
        return replace(line, spot_note=price) if isinstance(price, str) else replace(line, spot=price)
