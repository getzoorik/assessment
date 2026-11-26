"""Each disk's busiest minute of each hour of the last 14 days: Azure Monitor for managed disks, CloudWatch for EBS."""

from datetime import timedelta
from typing import Dict, Protocol

from zoorik.cloud.aws import AwsCli
from zoorik.cloud.azure import AzureCli
from zoorik.cloud.cli import Window
from zoorik.disks.model import Disk, Hourly, Load
from zoorik.format import SECONDS_PER_MINUTE
from zoorik.system.clock import Clock

METRIC_DAYS = 14
AZURE_METRICS = (
    "Composite Disk Read Operations/sec",
    "Composite Disk Write Operations/sec",
    "Composite Disk Read Bytes/sec",
    "Composite Disk Write Bytes/sec",
)
# CloudWatch keeps 1-minute EBS data points, so an hour's Maximum / 60 is its busiest minute, per second. Each metric
# says whether it counts operations (else bytes) and whether it counts writes.
EBS_METRICS = (
    ("VolumeReadOps", True, False),
    ("VolumeWriteOps", True, True),
    ("VolumeReadBytes", False, False),
    ("VolumeWriteBytes", False, True),
)
# An hour before its first metric is added.
QUIET = Load(0.0, 0.0, 0.0, 0.0)


class DiskMetrics(Protocol):
    """A cloud's disk metrics."""

    def hourly(self, disk: Disk, window: Window) -> Hourly:
        """Give the disk's busiest minute of each hour of window, reads and writes added."""


def metric_window(clock: Clock) -> Window:
    """Give the last 14 whole days, ending at the start of this hour."""
    end = clock.now().replace(minute=0, second=0, microsecond=0)
    return Window(end - timedelta(days=METRIC_DAYS), end)


def add_hour(hours: Dict[str, Load], stamp: str, ops: bool, write: bool, value: float) -> None:
    """Add one metric's figure to its hour: to the IOPS or the throughput, and for writes to its write figure too."""
    hour = hours.get(stamp, QUIET)
    written = value if write else 0.0
    if ops:
        hours[stamp] = Load(hour.iops + value, hour.throughput, hour.write_iops + written, hour.write_throughput)
    else:
        hours[stamp] = Load(hour.iops, hour.throughput + value, hour.write_iops, hour.write_throughput + written)


class AzureMetrics:
    """Managed disks' metrics, from Azure Monitor."""

    def __init__(self, cli: AzureCli) -> None:
        """Read the metrics through cli."""
        self._cli = cli

    def hourly(self, disk: Disk, window: Window) -> Hourly:
        """Give the disk's busiest minute (Maximum) of each hour of window, reads and writes added."""
        hours: Dict[str, Load] = {}
        for point in self._cli.monitor_maxima(disk.id, AZURE_METRICS, window):
            # An hour with no Maximum had no data, so it is left out.
            if point.maximum is not None:
                add_hour(hours, point.stamp, "Operations" in point.metric, "Write" in point.metric, point.maximum)
        return hours


class EbsMetrics:
    """EBS volumes' metrics, from CloudWatch."""

    def __init__(self, cli: AwsCli) -> None:
        """Read the metrics through cli."""
        self._cli = cli

    def hourly(self, disk: Disk, window: Window) -> Hourly:
        """Give the volume's busiest minute of each hour of window, reads and writes added."""
        hours: Dict[str, Load] = {}
        for metric, ops, write in EBS_METRICS:
            for point in self._cli.volume_maxima(disk.region, disk.id, metric, window):
                add_hour(hours, point.stamp, ops, write, point.maximum / SECONDS_PER_MINUTE)
        return hours
