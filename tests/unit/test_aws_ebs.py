"""EBS list prices."""

import json
import unittest
from typing import Dict, List, Optional, Sequence

from tests.replay.recording import Call
from tests.unit.clis import CloudTestCase, answer, aws
from zoorik.cloud.aws import AwsCli
from zoorik.cloud.started import StartedCommands
from zoorik.errors import PriceNotFound
from zoorik.pricing.aws_ebs import AwsEbsPrices, EbsRate, Served, ebs_rate, iops_band
from zoorik.pricing.disks import DiskSpec

REGION = "eu-west-1"


def product(attributes: Dict[str, str], unit: str, *prices: str) -> str:
    """Make one Price List product with its on-demand prices in unit, as the text the Price List gives."""
    dimensions = {
        f"dimension-{index}": {"unit": unit, "pricePerUnit": {"USD": price}, "beginRange": "0", "endRange": "Inf"}
        for index, price in enumerate(prices)
    }
    term = {"priceDimensions": dimensions}
    return json.dumps({"product": {"attributes": attributes}, "terms": {"OnDemand": {"term-one": term}}})


def family_call(family: str, price_list: Sequence[str]) -> Call:
    """Make the Price List read of one product family of the region, and its answer."""
    filters = json.dumps(
        [
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": REGION},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": family},
        ]
    )
    argv = aws(
        "pricing",
        "get-products",
        "--service-code",
        "AmazonEC2",
        "--format-version",
        "aws_v1",
        "--filters",
        filters,
        "--region",
        "us-east-1",
    )
    return answer(argv, {"PriceList": list(price_list)})


STORAGE = [
    product({"volumeApiName": "gp3"}, "GB-Mo", "0.0880000000"),
    product({"volumeApiName": "gp2"}, "GB-Mo", "0.1100000000"),
    product({"volumeApiName": "io1"}, "GB-Mo", "0.1380000000"),
    product({"volumeApiName": "io2"}, "GB-Mo", "0.1380000000"),
    product({"volumeApiName": "st1"}, "GB-Mo", "0.0500000000"),
    product({"volumeApiName": "sc1"}, "GB-Mo", "0.0168000000"),
    product({"volumeApiName": "sc1"}, "GB-Mo", "0.0170000000"),
    product({"volumeApiName": "standard"}, "GB-Mo", "0.0550000000"),
    product({"usagetype": "EBS:SnapshotUsage"}, "GB-Mo", "0.0500000000"),
]
OPERATIONS = [
    product({"volumeApiName": "gp3", "usagetype": "EBS:VolumeP-IOPS.gp3"}, "IOPS-Mo", "0.0055000000"),
    product({"volumeApiName": "io1", "usagetype": "EBS:VolumeP-IOPS.piops"}, "IOPS-Mo", "0.0720000000"),
    product({"volumeApiName": "io2", "usagetype": "EBS:VolumeP-IOPS.io2"}, "IOPS-Mo", "0.0720000000"),
    product({"volumeApiName": "io2", "usagetype": "EBS:VolumeP-IOPS.io2.tier2"}, "IOPS-Mo", "0.0504000000"),
    product({"volumeApiName": "io2", "usagetype": "EBS:VolumeP-IOPS.io2.tier3"}, "IOPS-Mo", "0.0352800000"),
    product({"volumeApiName": "standard", "usagetype": "EBS:VolumeIOUsage"}, "IOs", "0.0000000550"),
]
THROUGHPUT = [product({"volumeApiName": "gp3", "usagetype": "EBS:VolumeP-Throughput.gp3"}, "GiBps-mo", "40.9600000000")]


def volume(sku: str, size_gb: int, iops: Optional[int] = None, mbps: Optional[int] = None) -> DiskSpec:
    """Make a volume as its price sees it."""
    return DiskSpec(sku=sku, size_gb=size_gb, tier="", iops=iops, mbps=mbps)


class AwsEbsPricesTest(CloudTestCase):
    """AwsEbsPrices."""

    def setUp(self) -> None:
        """Serve the made-up price list of the region, one read per product family."""
        super().setUp()
        host, self.runner = self.host(
            [
                family_call("Storage", STORAGE),
                family_call("System Operation", OPERATIONS),
                family_call("Provisioned Throughput", THROUGHPUT),
            ]
        )
        self.prices = AwsEbsPrices(AwsCli(host, StartedCommands()))

    def test_each_type_charges_for_its_gb_and_what_it_charges_for_besides(self) -> None:
        """gp3 charges for the IOPS above 3,000 and the MB/s above 125; io1 every IOPS; io2 every IOPS in its bands."""
        cases = [
            # 100 x 0.088 + 1,000 x 0.0055 + 125 x (40.96 / 1,024) = 8.8 + 5.5 + 5.0.
            (volume("gp3", 100, iops=4000, mbps=250), 19.3),
            (volume("gp3", 100, iops=3000, mbps=125), 8.8),
            (volume("io1", 100, iops=1000), 85.8),
            # 100 x 0.138 + 32,000 x 0.072 + 8,000 x 0.0504 = 13.8 + 2,304 + 403.2.
            (volume("io2", 100, iops=40000), 2721.0),
            (volume("gp2", 100, iops=300), 11.0),
            (volume("st1", 500), 25.0),
        ]
        for spec, monthly in cases:
            self.assertEqual(self.prices.monthly(REGION, spec), monthly, spec)
        self.assertEqual(len(self.runner.runs), 3)

    def test_a_volume_with_no_price_says_why(self) -> None:
        """Magnetic volumes are charged per I/O request; a type with disagreeing products, or none, is not priced."""
        cases = [
            (volume("standard", 100), r"^magnetic \(standard\) volumes are charged for each I/O request; not priced$"),
            (volume("sc1", 500), r"^no sc1 list price in eu-west-1$"),
            (volume("", 100), r"^no unknown list price in eu-west-1$"),
        ]
        for spec, reason in cases:
            with self.assertRaisesRegex(PriceNotFound, reason):
                self.prices.monthly(REGION, spec)


class RateRulesTest(unittest.TestCase):
    """iops_band and ebs_rate."""

    def test_an_iops_price_starts_at_its_io2_tier(self) -> None:
        """io2's tiers 2 and 3 start at 32,000 and 64,000, a tier with no start is unknown, and no tier starts at 0."""
        self.assertEqual(iops_band("EBS:VolumeP-IOPS.io2"), 0)
        self.assertEqual(iops_band("EBS:VolumeP-IOPS.io2.tier2"), 32000)
        self.assertEqual(iops_band("EBS:VolumeP-IOPS.io2.tier3"), 64000)
        self.assertEqual(iops_band("EBS:VolumeP-IOPS.io2.tier9"), -1)
        self.assertEqual(iops_band("EBS:VolumeP-IOPS.piops"), 0)

    def test_a_type_is_priced_only_with_one_price_for_every_unit_it_charges_for(self) -> None:
        """An unknown band, no band from 0, or no throughput price leave the type unpriced."""
        bands: Dict[int, List[float]] = {0: [0.072], 32000: [0.0504]}
        self.assertEqual(ebs_rate(Served([0.138], bands), ("iops",)), EbsRate(0.138, {0: 0.072, 32000: 0.0504}))
        self.assertIsNone(ebs_rate(Served([0.138], {0: [0.072], -1: [0.03]}), ("iops",)))
        self.assertIsNone(ebs_rate(Served([0.138], {32000: [0.0504]}), ("iops",)))
        self.assertIsNone(ebs_rate(Served([0.088], {0: [0.0055]}), ("iops", "mbps")))
        self.assertIsNone(ebs_rate(None, ()))
        self.assertIsNone(ebs_rate(Served([0.1, 0.11]), ()))
        self.assertEqual(
            ebs_rate(Served([0.088], {0: [0.0055]}, [0.04]), ("iops", "mbps")),
            EbsRate(0.088, {0: 0.0055}, 3000, 0.04, 125),
        )
