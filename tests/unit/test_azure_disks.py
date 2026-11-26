"""Azure managed-disk list prices."""

import unittest
import urllib.parse
from typing import Dict, List, Optional

from tests.unit.clis import PriceServer
from zoorik.errors import PriceListUnreachable, PriceNotFound
from zoorik.pricing.azure_disks import AzureDiskPrices, graduated, size_tier
from zoorik.pricing.disks import DiskSpec
from zoorik.pricing.retail import RETAIL_PRICES_URL

QUERY = (
    "armRegionName eq 'westeurope' and priceType eq 'Consumption' and (productName eq 'Premium SSD Managed Disks' "
    "or productName eq 'Standard SSD Managed Disks' or productName eq 'Standard HDD Managed Disks' "
    "or productName eq 'Azure Premium SSD v2' or productName eq 'Ultra Disks')"
)
URL = RETAIL_PRICES_URL + "?" + urllib.parse.urlencode({"$filter": QUERY})


def item(product: str, meter: str, unit: str, price: float, tier_minimum: float = 0.0) -> Dict[str, object]:
    """Make one retail item of a disk meter."""
    return {
        "armSkuName": "",
        "productName": product,
        "meterName": meter,
        "unitOfMeasure": unit,
        "unitPrice": price,
        "tierMinimumUnits": tier_minimum,
    }


ITEMS: List[Dict[str, object]] = [
    item("Premium SSD Managed Disks", "P10 LRS Disk", "1/Month", 19.71),
    item("Premium SSD Managed Disks", "P30 LRS Disk", "1/Month", 135.17),
    item("Premium SSD Managed Disks", "P10 ZRS Disk", "1/Month", 29.57),
    item("Premium SSD Managed Disks", "P10 LRS Disk Mount", "1/Hour", 0.5),
    item("Standard HDD Managed Disks", "S4 LRS Disk", "1/Month", 1.54),
    item("Standard HDD Managed Disks", "Disk Operations", "10K", 0.0005),
    item("Azure Premium SSD v2", "Premium LRS Provisioned Capacity", "1 GiB/Hour", 0.0001),
    item("Azure Premium SSD v2", "Premium LRS Provisioned IOPS", "1/Hour", 0.0),
    item("Azure Premium SSD v2", "Premium LRS Provisioned IOPS", "1/Hour", 0.00001, tier_minimum=3000.0),
    item("Azure Premium SSD v2", "Premium LRS Provisioned Throughput (MBps)", "1/Hour", 0.0),
    item("Azure Premium SSD v2", "Premium LRS Provisioned Throughput (MBps)", "1/Hour", 0.0001, tier_minimum=125.0),
]


def disk(sku: str, size_gb: int, tier: str = "", iops: Optional[int] = None, mbps: Optional[int] = None) -> DiskSpec:
    """Make a disk as its price sees it."""
    return DiskSpec(sku=sku, size_gb=size_gb, tier=tier, iops=iops, mbps=mbps)


class AzureDiskPricesTest(unittest.TestCase):
    """AzureDiskPrices."""

    def setUp(self) -> None:
        """Serve the made-up price list of westeurope."""
        self.server = PriceServer({URL: {"Items": ITEMS}})
        self.prices = AzureDiskPrices(self.server)

    def test_a_disk_is_priced_at_the_smallest_tier_that_holds_it(self) -> None:
        """Premium SSD and Standard HDD are priced by size tier and redundancy; Standard HDD starts at S4."""
        self.assertEqual(self.prices.monthly("westeurope", disk("Premium_LRS", 100)), 19.71)
        self.assertEqual(self.prices.monthly("westeurope", disk("Premium_ZRS", 100)), 29.57)
        self.assertEqual(self.prices.monthly("westeurope", disk("Premium_LRS", 1000)), 135.17)
        self.assertEqual(self.prices.monthly("westeurope", disk("Standard_LRS", 20)), 1.54)

    def test_a_higher_performance_tier_sets_the_price(self) -> None:
        """A Premium SSD set to a higher performance tier is priced at it; a smaller one changes nothing."""
        self.assertEqual(self.prices.monthly("westeurope", disk("Premium_LRS", 100, tier="P30")), 135.17)
        self.assertEqual(self.prices.monthly("westeurope", disk("Premium_LRS", 1000, tier="P10")), 135.17)

    def test_a_provisioned_disk_is_charged_each_hour_for_what_it_is_provisioned_with(self) -> None:
        """Premium SSD v2 charges for GiB, IOPS above 3,000 and MB/s above 125 each hour, 730 hours a month."""
        # 256 x 0.0001 + 2,000 x 0.00001 + 75 x 0.0001 = 0.0531 an hour; x 730 = 38.763.
        self.assertEqual(self.prices.monthly("westeurope", disk("PremiumV2_LRS", 256, iops=5000, mbps=200)), 38.76)

    def test_each_price_list_is_read_once_per_region(self) -> None:
        """Every disk of a region is priced from one read of its price list."""
        self.prices.monthly("westeurope", disk("Premium_LRS", 100))
        self.prices.monthly("westeurope", disk("Standard_LRS", 20))
        self.assertEqual(self.server.urls, [URL])

    def test_a_disk_with_no_price_says_why(self) -> None:
        """Each reason a disk has no price is its own PriceNotFound."""
        cases = [
            (disk("Standard_GRS", 100), r"^Standard_GRS disks are not priced$"),
            (disk("", 100), r"^unknown disks are not priced$"),
            (disk("Premium_LRS", 40000), r"^40,000 GB is above the largest Premium SSD tier$"),
            (disk("StandardSSD_LRS", 100), r"^no list price for E10 LRS in westeurope$"),
            (disk("PremiumV2_LRS", 256, iops=5000), r"^provisioned IOPS or MB/s not reported$"),
            (disk("UltraSSD_LRS", 256, iops=5000, mbps=200), r"^no Ultra Disk list price in westeurope$"),
        ]
        for spec, reason in cases:
            with self.assertRaisesRegex(PriceNotFound, reason):
                self.prices.monthly("westeurope", spec)

    def test_a_price_list_that_failed_is_kept_for_the_region(self) -> None:
        """A region whose price list could not be read raises that failure for each disk, and is not read again."""
        server = PriceServer({URL: PriceListUnreachable("the public price list (prices.azure.com) is not reachable")})
        prices = AzureDiskPrices(server)
        for size in (100, 200):
            with self.assertRaises(PriceListUnreachable):
                prices.monthly("westeurope", disk("Premium_LRS", size))
        self.assertEqual(len(server.urls), 1)


class TierRulesTest(unittest.TestCase):
    """graduated and size_tier."""

    def test_each_graduated_tier_prices_its_own_units(self) -> None:
        """A tier's price applies from its first unit to the next tier's."""
        self.assertEqual(graduated(15, {0.0: 1.0, 10.0: 0.5}), 12.5)
        self.assertEqual(graduated(5, {0.0: 1.0, 10.0: 0.5}), 5.0)
        self.assertEqual(graduated(0, {0.0: 1.0}), 0.0)

    def test_the_size_tier_is_the_smallest_at_or_above_the_size(self) -> None:
        """Sizes go up to the next tier; Standard HDD has no tier below S4; above the largest there is none."""
        self.assertEqual([size_tier("P", size) for size in (1, 4, 5, 1024, 32767)], ["P1", "P1", "P2", "P30", "P80"])
        self.assertEqual(size_tier("S", 1), "S4")
        self.assertIsNone(size_tier("E", 32768))
