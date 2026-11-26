"""What a disk's list price depends on, and the protocol both clouds' disk price lists meet."""

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class DiskSpec:
    """A disk as its list price sees it: its type and size, and the IOPS and MB/s it is provisioned with."""

    sku: str
    size_gb: int
    # The performance tier set on an Azure Premium SSD, such as P30; empty when none is set, and on AWS.
    tier: str
    iops: Optional[int]
    mbps: Optional[int]


class DiskPrices(Protocol):
    """A cloud's disk list prices."""

    def monthly(self, region: str, disk: DiskSpec) -> float:
        """Give the disk's list price per month in region; PriceNotFound with the reason when it has none."""
