"""Machine list prices per hour."""

import json
import urllib.parse
from typing import Dict, List

from tests.replay.recording import Call
from tests.unit.clis import CloudTestCase, PriceServer, answer, aws, failing
from zoorik.cloud.aws import AwsCli
from zoorik.cloud.started import StartedCommands
from zoorik.errors import AccessDenied, PriceListUnreachable, PriceNotFound
from zoorik.pricing.machines import HOURS_PER_MONTH, AwsMachinePrices, AzureMachinePrices, Capacity
from zoorik.pricing.retail import RETAIL_PRICES_URL

REGION = "westeurope"


def retail_url(query: str) -> str:
    """Give the Azure Retail Prices API's URL for an OData filter."""
    return RETAIL_PRICES_URL + "?" + urllib.parse.urlencode({"$filter": query})


def vm_query(*skus: str) -> str:
    """Give the retail prices filter for some machine types of the region."""
    names = " or ".join(f"armSkuName eq '{sku}'" for sku in skus)
    return (
        f"armRegionName eq '{REGION}' and serviceName eq 'Virtual Machines' and priceType eq 'Consumption' "
        f"and ({names})"
    )


def item(sku: str, meter: str, price: float, product: str = "Virtual Machines Dsv5 Series") -> Dict[str, object]:
    """Make one retail item of a machine type, priced per hour."""
    return {
        "armSkuName": sku,
        "productName": product,
        "meterName": meter,
        "unitOfMeasure": "1 Hour",
        "unitPrice": price,
        "tierMinimumUnits": 0.0,
        "currencyCode": "USD",
    }


class AzureMachinePricesTest(CloudTestCase):
    """AzureMachinePrices."""

    def test_regular_and_spot_meters_come_in_one_query_per_batch(self) -> None:
        """Two machine types are read in one query; each capacity's price is its lowest positive Linux meter."""
        items = [
            item("Standard_D2s_v5", "D2s v5", 0.101),
            item("Standard_D2s_v5", "D2s v5", 0.096),
            item("Standard_D2s_v5", "D2s v5", 0.188, product="Virtual Machines Dsv5 Series Windows"),
            item("Standard_D2s_v5", "D2s v5 Low Priority", 0.019),
            item("Standard_D2s_v5", "D2s v5 Spot", 0.0192),
            item("Standard_D2s_v5", "D2s v5", 0.0),
            {**item("Standard_D2s_v5", "D2s v5", 0.05), "unitOfMeasure": "1/Month"},
            item("Standard_D4s_v5", "D4s v5", 0.192),
        ]
        server = PriceServer({retail_url(vm_query("Standard_D2s_v5", "Standard_D4s_v5")): {"Items": items}})
        prices = AzureMachinePrices(server)
        prices.read(REGION, ["Standard_D4s_v5", "Standard_D2s_v5"])
        self.assertEqual(prices.hourly(REGION, "Standard_D2s_v5", Capacity.REGULAR), 0.096)
        self.assertEqual(prices.hourly(REGION, "Standard_D2s_v5", Capacity.SPOT), 0.0192)
        self.assertEqual(prices.hourly(REGION, "Standard_D4s_v5", Capacity.REGULAR), 0.192)
        with self.assertRaisesRegex(PriceNotFound, r"^no spot price for Standard_D4s_v5 in westeurope$"):
            prices.hourly(REGION, "Standard_D4s_v5", Capacity.SPOT)
        self.assertEqual(len(server.urls), 1)

    def test_a_machine_type_with_no_price_is_not_found(self) -> None:
        """A type the price list doesn't price raises PriceNotFound."""
        server = PriceServer({retail_url(vm_query("Standard_Z1")): {"Items": []}})
        with self.assertRaisesRegex(PriceNotFound, r"^no list price for Standard_Z1 in westeurope$"):
            AzureMachinePrices(server).hourly(REGION, "Standard_Z1", Capacity.REGULAR)

    def test_types_go_20_to_a_query_and_pages_are_followed(self) -> None:
        """21 types take two queries, and a page's next link is followed."""
        skus = sorted(f"Standard_D{number}" for number in range(21))
        next_page = retail_url(vm_query(*skus[:20])) + "&%24skip=100"
        server = PriceServer(
            {
                retail_url(vm_query(*skus[:20])): {"Items": [], "NextPageLink": next_page},
                next_page: {"Items": [item(skus[0], "D0", 0.5)], "NextPageLink": None},
                retail_url(vm_query(skus[20])): {"Items": []},
            }
        )
        prices = AzureMachinePrices(server)
        prices.read(REGION, skus)
        self.assertEqual(prices.hourly(REGION, skus[0], Capacity.REGULAR), 0.5)
        self.assertEqual(len(server.urls), 3)

    def test_a_failed_batch_is_kept_for_each_of_its_types(self) -> None:
        """A batch whose read failed raises that failure for each of its types, and is not read again."""
        failure = PriceListUnreachable("the public price list (prices.azure.com) is not reachable")
        server = PriceServer({retail_url(vm_query("Standard_D2s_v5", "Standard_D4s_v5")): failure})
        prices = AzureMachinePrices(server)
        prices.read(REGION, ["Standard_D2s_v5", "Standard_D4s_v5"])
        for sku in ("Standard_D2s_v5", "Standard_D4s_v5", "Standard_D2s_v5"):
            with self.assertRaisesRegex(PriceListUnreachable, "is not reachable"):
                prices.hourly(REGION, sku, Capacity.REGULAR)
        self.assertEqual(len(server.urls), 1)

    def test_a_month_is_730_hours(self) -> None:
        """A month of list price is 730 hours of it."""
        self.assertEqual(HOURS_PER_MONTH, 730)


ON_DEMAND = json.dumps(
    [
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": "eu-west-1"},
        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
        {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
        {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
        {"Type": "TERM_MATCH", "Field": "operation", "Value": "RunInstances"},
    ]
)
PRICE_LIST = aws(
    "pricing",
    "get-products",
    "--service-code",
    "AmazonEC2",
    "--format-version",
    "aws_v1",
    "--filters",
    ON_DEMAND,
    "--region",
    "us-east-1",
)


def product(machine_type: str, *prices: str) -> str:
    """Make one Price List product of a machine type, with its on-demand prices per hour."""
    dimensions = {
        f"dimension-{index}": {"unit": "Hrs", "pricePerUnit": {"USD": price}, "beginRange": "0", "endRange": "Inf"}
        for index, price in enumerate(prices)
    }
    term = {"priceDimensions": dimensions}
    return json.dumps(
        {"product": {"attributes": {"instanceType": machine_type}}, "terms": {"OnDemand": {"term-one": term}}}
    )


def spot_history(region: str, machine_type: str, entries: List[Dict[str, str]]) -> Call:
    """Make the spot price history call of a machine type, at the run's start, and its answer."""
    argv = aws(
        "ec2",
        "describe-spot-price-history",
        "--instance-types",
        machine_type,
        "--product-descriptions",
        "Linux/UNIX",
        "--start-time",
        "2026-01-05T09:30:00Z",
        "--region",
        region,
    )
    return answer(argv, {"SpotPriceHistory": entries})


class AwsMachinePricesTest(CloudTestCase):
    """AwsMachinePrices."""

    def prices(self, calls: List[Call]) -> AwsMachinePrices:
        """Make the AWS prices, reading through aws from calls."""
        host, self.runner = self.host(calls)
        return AwsMachinePrices(AwsCli(host, StartedCommands()), self.clock)

    def test_the_region_is_read_once_for_every_type(self) -> None:
        """One Price List read prices every type of the region at its lowest positive on-demand price."""
        price_list = [product("m5.large", "0.1100000000", "0.0000000000"), product("m5.large", "0.1070000000")]
        price_list += [product("m5.xlarge", "0.2140000000"), product("t3.nano", "0.0000000000")]
        prices = self.prices([answer(PRICE_LIST, {"PriceList": price_list})])
        prices.read("eu-west-1", ["m5.large", "m5.xlarge"])
        self.assertEqual(prices.hourly("eu-west-1", "m5.large", Capacity.REGULAR), 0.107)
        self.assertEqual(prices.hourly("eu-west-1", "m5.xlarge", Capacity.REGULAR), 0.214)
        with self.assertRaisesRegex(PriceNotFound, r"^no list price for t3.nano in eu-west-1$"):
            prices.hourly("eu-west-1", "t3.nano", Capacity.REGULAR)
        self.assertEqual(len(self.runner.runs), 1)

    def test_a_failed_region_read_is_kept(self) -> None:
        """A failed Price List read is kept, not raised, by the read ahead; each price of the region raises it."""
        denied = "An error occurred (AccessDeniedException) when calling the GetProducts operation"
        prices = self.prices([failing(PRICE_LIST, denied)])
        prices.read("eu-west-1", ["m5.large"])
        for machine_type in ("m5.large", "m5.xlarge"):
            with self.assertRaisesRegex(AccessDenied, "^access denied$"):
                prices.hourly("eu-west-1", machine_type, Capacity.REGULAR)
        self.assertEqual(len(self.runner.runs), 1)

    def test_the_spot_price_averages_each_zones_latest_price(self) -> None:
        """Each zone's latest price counts once, whatever order the history comes in; the price is read once."""
        entries = [
            {"AvailabilityZone": "eu-west-1a", "SpotPrice": "0.040000", "Timestamp": "2026-01-05T09:00:00+00:00"},
            {"AvailabilityZone": "eu-west-1b", "SpotPrice": "0.044000", "Timestamp": "2026-01-05T07:00:00+00:00"},
            {"AvailabilityZone": "eu-west-1a", "SpotPrice": "0.050000", "Timestamp": "2026-01-05T08:00:00+00:00"},
        ]
        prices = self.prices([spot_history("eu-west-1", "m5.large", entries)])
        self.assertAlmostEqual(prices.hourly("eu-west-1", "m5.large", Capacity.SPOT), 0.042, places=12)
        self.assertAlmostEqual(prices.hourly("eu-west-1", "m5.large", Capacity.SPOT), 0.042, places=12)
        self.assertEqual(len(self.runner.runs), 1)

    def test_a_type_with_no_spot_price_is_not_found(self) -> None:
        """A spot history with no entries raises PriceNotFound."""
        prices = self.prices([spot_history("eu-west-2", "m5.large", [])])
        with self.assertRaisesRegex(PriceNotFound, r"^no spot price for m5.large in eu-west-2$"):
            prices.hourly("eu-west-2", "m5.large", Capacity.SPOT)
