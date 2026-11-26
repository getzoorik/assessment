"""Machine list prices per hour: Azure's retail prices, and AWS's Price List and spot price history.

The rule for every machine type is the lowest positive price the price list gives it. A spot price on AWS is the
average, over the region's zones, of each zone's latest price; the region's spot price history is read once.
"""

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Collection, Dict, List, Protocol, Tuple, Union

from zoorik.cloud.aws import AwsCli
from zoorik.errors import AssessmentError, PriceNotFound
from zoorik.pricing.retail import RetailItem, retail_items
from zoorik.system.clock import Clock
from zoorik.system.http import HttpClient
from zoorik.system.memo import Memo

HOURS_PER_MONTH = 730
# Machine types asked for in one retail prices query, which keeps its URL short.
SKU_BATCH = 20
# A Linux machine on shared hardware with no software in its price, as the Price List names it.
ON_DEMAND_TERMS = (
    ("operatingSystem", "Linux"),
    ("tenancy", "Shared"),
    ("preInstalledSw", "NA"),
    ("capacitystatus", "Used"),
    ("operation", "RunInstances"),
)


class Capacity(Enum):
    """How a machine is bought: at its regular price, or as spot capacity the cloud may take back."""

    REGULAR = "regular"
    SPOT = "spot"


class MachinePrices(Protocol):
    """A cloud's machine list prices."""

    def read(self, region: str, machine_types: Collection[str]) -> None:
        """Read ahead the prices hourly gives for machine_types in region, in as few calls as the price list allows.

        A failure is kept, and hourly raises it for each machine type it covers.
        """

    def hourly(self, region: str, machine_type: str, capacity: Capacity) -> float:
        """Give the machine type's hourly list price; PriceNotFound when the price list has none."""


@dataclass(frozen=True)
class Meters:
    """The hourly Linux prices one Azure machine type has in a region, regular and spot."""

    regular: Tuple[float, ...]
    spot: Tuple[float, ...]


def missing(machine_type: str, region: str, capacity: Capacity) -> PriceNotFound:
    """Make the failure for a machine type the price list has no price for."""
    kind = "spot" if capacity is Capacity.SPOT else "list"
    return PriceNotFound(f"no {kind} price for {machine_type} in {region}")


def linux_hourly(item: RetailItem) -> bool:
    """Tell whether a retail item is a Linux machine's price per hour; Windows and low-priority meters are not."""
    return (
        "Windows" not in item.product and "Low Priority" not in item.meter and item.unit == "1 Hour" and item.price > 0
    )


class AzureMachinePrices:
    """Azure's machine prices, from the Azure Retail Prices API, read SKU_BATCH machine types at a time."""

    def __init__(self, http: HttpClient) -> None:
        """Read prices through http."""
        self._http = http
        self._lock = threading.Lock()
        self._meters: Dict[Tuple[str, str], Union[Meters, AssessmentError]] = {}

    def read(self, region: str, machine_types: Collection[str]) -> None:
        """Read the meters of the machine types not read yet; a failed batch is kept for each of its types."""
        with self._lock:
            unread = sorted({kind for kind in machine_types if (region, kind) not in self._meters})
        for start in range(0, len(unread), SKU_BATCH):
            batch = unread[start : start + SKU_BATCH]
            found = self._batch(region, batch)
            with self._lock:
                for kind in batch:
                    self._meters[(region, kind)] = found if isinstance(found, AssessmentError) else found[kind]

    def hourly(self, region: str, machine_type: str, capacity: Capacity) -> float:
        """Give the lowest regular or spot meter of the machine type."""
        self.read(region, [machine_type])
        with self._lock:
            meters = self._meters[(region, machine_type)]
        if isinstance(meters, AssessmentError):
            raise type(meters)(str(meters))
        prices = meters.spot if capacity is Capacity.SPOT else meters.regular
        if not prices:
            raise missing(machine_type, region, capacity)
        return min(prices)

    def _batch(self, region: str, batch: List[str]) -> Union[Dict[str, Meters], AssessmentError]:
        """Read the meters of a batch of machine types in one query, or the failure that stopped the read."""
        skus = " or ".join(f"armSkuName eq '{kind}'" for kind in batch)
        query = (
            f"armRegionName eq '{region}' and serviceName eq 'Virtual Machines' and priceType eq 'Consumption' "
            f"and ({skus})"
        )
        try:
            items = retail_items(self._http, query)
        except AssessmentError as error:
            return error
        regular: Dict[str, List[float]] = {kind: [] for kind in batch}
        spot: Dict[str, List[float]] = {kind: [] for kind in batch}
        for item in items:
            if item.sku in regular and linux_hourly(item):
                (spot if item.meter.endswith("Spot") else regular)[item.sku].append(item.price)
        return {kind: Meters(tuple(regular[kind]), tuple(spot[kind])) for kind in batch}


class AwsMachinePrices:
    """AWS's machine prices: the region's on-demand Price List and its spot price history, each read once."""

    def __init__(self, aws: AwsCli, clock: Clock) -> None:
        """Read prices through aws; spot prices are those in effect at clock's time."""
        self._aws = aws
        self._clock = clock
        self._regular: Memo[str, Dict[str, float]] = Memo(self._read_region)
        self._spot: Memo[str, Dict[str, float]] = Memo(self._read_spot)

    def read(self, region: str, machine_types: Collection[str]) -> None:
        """Read the region's on-demand price list, which holds every machine type; spot prices wait for hourly."""
        self._regular.fill(region)

    def hourly(self, region: str, machine_type: str, capacity: Capacity) -> float:
        """Give the machine type's lowest on-demand price, or its spot price."""
        prices = self._spot.get(region) if capacity is Capacity.SPOT else self._regular.get(region)
        if machine_type not in prices:
            raise missing(machine_type, region, capacity)
        return prices[machine_type]

    def _read_region(self, region: str) -> Dict[str, float]:
        """Read the lowest positive on-demand price of every machine type in the region."""
        found: Dict[str, List[float]] = {}
        for product in self._aws.price_products((("regionCode", region), *ON_DEMAND_TERMS)):
            positive = [dimension.usd for dimension in product.on_demand if dimension.usd > 0]
            if positive:
                found.setdefault(product.attribute("instanceType"), []).extend(positive)
        return {kind: min(prices) for kind, prices in found.items()}

    def _read_spot(self, region: str) -> Dict[str, float]:
        """Read each machine type's spot price: the average over the region's zones of each zone's latest price."""
        latest: Dict[str, Dict[str, float]] = {}
        for entry in sorted(self._aws.spot_prices(region, self._clock.now()), key=lambda each: each.stamp):
            latest.setdefault(entry.machine_type, {})[entry.zone] = entry.price
        return {kind: sum(zones.values()) / len(zones) for kind, zones in latest.items()}
