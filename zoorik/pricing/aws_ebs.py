"""EBS list prices per month, from the AWS Price List, read once per region.

Each volume type charges for its GB. gp3 also charges for the IOPS above 3,000 and the MB/s above 125; io1 and io2
charge for every IOPS, io2 in bands whose tiers 2 and 3 start at 32,000 and 64,000. A type is priced only when the
price list gives one price for every unit it charges for.
"""

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from zoorik.cloud.aws import AwsCli, PriceProduct
from zoorik.errors import PriceNotFound
from zoorik.format import MIB_PER_GIB, cents
from zoorik.pricing.disks import DiskSpec
from zoorik.system.memo import Memo

EBS_PRICE_FAMILIES = ("Storage", "System Operation", "Provisioned Throughput")
# What each volume type charges for besides its GB.
EBS_CHARGES: Mapping[str, Tuple[str, ...]] = {
    "gp3": ("iops", "mbps"),
    "gp2": (),
    "io1": ("iops",),
    "io2": ("iops",),
    "st1": (),
    "sc1": (),
}
IO2_TIER_STARTS = {2: 32000, 3: 64000}
IO2_TIER = re.compile(r"\.tier(\d+)$")
# An io2 tier this table has no start for, which leaves its type unpriced.
UNKNOWN_BAND = -1
GP3_BASE_IOPS = 3000
GP3_BASE_MBPS = 125
MAGNETIC = "magnetic (standard) volumes are charged for each I/O request; not priced"


@dataclass(frozen=True)
class EbsRate:
    """One volume type's prices per month: per GB, per IOPS by the band it starts at, per MB/s, above what it holds."""

    gib: float
    iops: Mapping[int, float] = field(default_factory=dict)
    included_iops: int = 0
    mbps: Optional[float] = None
    included_mbps: int = 0


@dataclass
class Served:
    """The prices the Price List serves for one volume type, by the unit each one is for."""

    gib: List[float] = field(default_factory=list)
    iops: Dict[int, List[float]] = field(default_factory=dict)
    mbps: List[float] = field(default_factory=list)


def single(prices: Sequence[float]) -> Optional[float]:
    """Give the one price a unit has; None when it has none, or when two products disagree."""
    return prices[0] if prices and all(price == prices[0] for price in prices) else None


def iops_band(usage: str) -> int:
    """Give the IOPS a price starts at: its io2 tier's start, or 0 for a price with no tier."""
    tier = IO2_TIER.search(usage)
    if tier is None:
        return 0
    return IO2_TIER_STARTS.get(int(tier.group(1)), UNKNOWN_BAND)


def add_product(served: Dict[str, Served], family: str, product: PriceProduct) -> None:
    """Add each positive on-demand price of one product to its volume type, by the unit it is for."""
    kind = product.attributes.get("volumeApiName")
    if kind is None:
        # Products with no volume type, such as snapshots, price no volume.
        return
    found = served.setdefault(kind.lower(), Served())
    for dimension in product.on_demand:
        unit = dimension.unit.lower()
        if dimension.usd <= 0:
            continue
        if family == "Storage" and unit == "gb-mo":
            found.gib.append(dimension.usd)
        elif family == "System Operation" and unit == "iops-mo":
            found.iops.setdefault(iops_band(product.attribute("usagetype")), []).append(dimension.usd)
        elif family == "Provisioned Throughput" and unit == "gibps-mo":
            found.mbps.append(dimension.usd / MIB_PER_GIB)


def iops_bands(served: Dict[int, List[float]]) -> Optional[Dict[int, float]]:
    """Give the price of each IOPS band; None when there are none, or a band is unknown or has no one price."""
    bands: Dict[int, float] = {}
    for start, prices in served.items():
        price = single(prices)
        if start < 0 or price is None:
            return None
        bands[start] = price
    return bands or None


def ebs_rate(served: Optional[Served], charges: Tuple[str, ...]) -> Optional[EbsRate]:
    """Give a volume type's rate when the price list has one price for every unit it charges for, else None."""
    gib = None if served is None else single(served.gib)
    if served is None or gib is None:
        return None
    if "iops" not in charges:
        return EbsRate(gib=gib)
    bands = iops_bands(served.iops)
    if bands is None or min(bands) != 0:
        return None
    if "mbps" not in charges:
        return EbsRate(gib=gib, iops=bands)
    mbps = single(served.mbps)
    if mbps is None:
        return None
    return EbsRate(gib, bands, GP3_BASE_IOPS, mbps, GP3_BASE_MBPS)


def ebs_monthly(rate: EbsRate, disk: DiskSpec) -> float:
    """Price one volume: its GB, plus each IOPS and MB/s above what its type includes, at its band's price."""
    usd = rate.gib * disk.size_gb
    if disk.iops is not None and disk.iops > rate.included_iops:
        bands = sorted(rate.iops.items())
        for index, (start, price) in enumerate(bands):
            low = max(start, rate.included_iops)
            high = bands[index + 1][0] if index + 1 < len(bands) else math.inf
            if disk.iops > low:
                usd += (min(disk.iops, high) - low) * price
    if disk.mbps is not None and rate.mbps is not None and disk.mbps > rate.included_mbps:
        usd += (disk.mbps - rate.included_mbps) * rate.mbps
    return cents(usd)


class AwsEbsPrices:
    """AWS's EBS prices."""

    def __init__(self, aws: AwsCli) -> None:
        """Read each region's prices through aws, once."""
        self._aws = aws
        self._regions: Memo[str, Dict[str, EbsRate]] = Memo(self._read)

    def monthly(self, region: str, disk: DiskSpec) -> float:
        """Give the volume's list price per month from its type, size, IOPS and MB/s."""
        rate = self._regions.get(region).get(disk.sku.lower())
        if rate is None:
            if disk.sku == "standard":
                raise PriceNotFound(MAGNETIC)
            raise PriceNotFound(f"no {disk.sku or 'unknown'} list price in {region}")
        return ebs_monthly(rate, disk)

    def _read(self, region: str) -> Dict[str, EbsRate]:
        """Read the rate of every volume type the region's price list prices in full."""
        served: Dict[str, Served] = {}
        for family in EBS_PRICE_FAMILIES:
            for product in self._aws.price_products((("regionCode", region), ("productFamily", family))):
                add_product(served, family, product)
        rates = {kind: ebs_rate(served.get(kind), charges) for kind, charges in EBS_CHARGES.items()}
        return {kind: rate for kind, rate in rates.items() if rate is not None}
