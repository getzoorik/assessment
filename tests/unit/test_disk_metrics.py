"""Disk metrics: each disk's busiest minute of each hour of the last 14 days."""

import unittest
from typing import Dict

from tests.replay.disks import (
    CLOUDWATCH_HOUR,
    END,
    HOUR_ONE,
    HOUR_TWO,
    START,
    cloudwatch,
    disk_id,
    hour,
    monitor,
)
from tests.replay.fake_cloud import VirtualClock
from tests.replay.scenarios import FACTS, REGION
from tests.unit.clis import CloudTestCase
from zoorik.cloud.aws import AwsCli
from zoorik.cloud.azure import AzureCli
from zoorik.cloud.cli import utc_text
from zoorik.cloud.keys import AWS, AZURE
from zoorik.cloud.started import StartedCommands
from zoorik.disks.metrics import AzureMetrics, EbsMetrics, add_hour, metric_window
from zoorik.disks.model import Disk, Load

MANAGED_DISK = Disk(disk_id("data-one"), AZURE, REGION, "Premium_LRS", 1024, "P30", 5000, 200, False, False)
VOLUME = Disk("vol-01", AWS, "eu-west-1", "gp2", 500, "", 1500, None, False, False)


class WindowTest(unittest.TestCase):
    """metric_window and add_hour."""

    def test_the_window_is_the_14_days_to_the_start_of_this_hour(self) -> None:
        """A run at 09:30 reads from 09:00 fourteen days before to 09:00 today."""
        window = metric_window(VirtualClock(FACTS.started))
        self.assertEqual((utc_text(window.start), utc_text(window.end)), (START, END))

    def test_a_figure_adds_to_its_hours_operations_or_bytes_and_writes_to_the_write_figure(self) -> None:
        """Reads add to the IOPS or the throughput; writes add to the write figure as well."""
        hours: Dict[str, Load] = {}
        add_hour(hours, HOUR_ONE, True, False, 300)
        add_hour(hours, HOUR_ONE, True, True, 200)
        add_hour(hours, HOUR_ONE, False, True, 10e6)
        self.assertEqual(hours, {HOUR_ONE: Load(500, 10e6, 200, 10e6)})


class AzureMetricsTest(CloudTestCase):
    """AzureMetrics."""

    def test_an_hour_adds_reads_and_writes_and_an_hour_with_no_maximum_is_left_out(self) -> None:
        """The busiest minute of each metric adds up; an hour that has only an Average had no Maximum."""
        quiet = hour(HOUR_TWO, (9000, 9000), (9e8, 9e8), field="average")
        host, _ = self.host([monitor("data-one", [hour(HOUR_ONE, (300, 200), (20e6, 10e6)), quiet])])
        metrics = AzureMetrics(AzureCli(host, StartedCommands()))
        hourly = metrics.hourly(MANAGED_DISK, metric_window(self.clock))
        self.assertEqual(hourly, {HOUR_ONE: Load(500, 30e6, 200, 10e6)})


class EbsMetricsTest(CloudTestCase):
    """EbsMetrics."""

    def test_an_hours_maximum_is_its_busiest_minute_so_a_sixtieth_is_per_second(self) -> None:
        """6,000 reads and 6,000 writes in the busiest minute are 100 a second each; bytes alike."""
        host, runner = self.host(cloudwatch("vol-01", (6000, 6000, 600e6, 300e6)))
        hourly = EbsMetrics(AwsCli(host, StartedCommands())).hourly(VOLUME, metric_window(self.clock))
        self.assertEqual(hourly, {CLOUDWATCH_HOUR: Load(200, 15e6, 100, 5e6)})
        self.assertEqual(len(runner.runs), 4)
