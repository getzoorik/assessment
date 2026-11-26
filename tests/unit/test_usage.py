"""Usage: kubectl top samples over a window, kept as each container's peak."""

import unittest

from tests.replay.clusters import SAMPLES, top
from zoorik.errors import UnexpectedResponse
from zoorik.kubectl.usage import (
    MEBIBYTE,
    NO_DATA,
    SAMPLE_INTERVAL,
    ContainerKey,
    Peak,
    SampleWindow,
    Usage,
    peaks,
    top_table,
    usage_of,
)

WEB_APP = ContainerKey("shop", "web-6d4b75cb6d-abcde", "app")
REPORT = ContainerKey("batch", "report-28100000-x7k2p", "report")


class SampleWindowTest(unittest.TestCase):
    """SampleWindow."""

    def test_a_window_takes_a_sample_at_its_start_and_every_30_seconds(self) -> None:
        """Three minutes take 7 samples, fifteen take 31, and no minutes take the one at the start."""
        self.assertEqual(SAMPLE_INTERVAL, 30)
        self.assertEqual(
            [(SampleWindow(m).seconds, SampleWindow(m).samples) for m in (0, 3, 15)], [(0, 1), (180, 7), (900, 31)]
        )


class TopTableTest(unittest.TestCase):
    """top_table."""

    def test_each_line_is_a_containers_cpu_and_memory(self) -> None:
        """CPU is read in millicores and memory in bytes; blank lines are passed over."""
        table = top_table(
            "\n" + top("shop  web-6d4b75cb6d-abcde  app  120m  200Mi", "batch  report-28100000-x7k2p  report  0m  1Mi")
        )
        self.assertEqual(table, {WEB_APP: Peak(120, 200 * MEBIBYTE), REPORT: Peak(0, MEBIBYTE)})

    def test_a_container_listed_twice_keeps_its_highest(self) -> None:
        """Two lines for one container keep the higher CPU and the higher memory, each on its own."""
        table = top_table(top("shop web-6d4b75cb6d-abcde app 120m 300Mi", "shop web-6d4b75cb6d-abcde app 150m 200Mi"))
        self.assertEqual(table, {WEB_APP: Peak(150, 300 * MEBIBYTE)})

    def test_a_line_that_does_not_parse_is_unexpected(self) -> None:
        """A header, a missing column or a unit kubectl top never writes is refused, without the line's names."""
        lines = (
            "NAMESPACE  POD  NAME  CPU(cores)  MEMORY(bytes)",
            "shop  web-6d4b75cb6d-abcde  app  120m",
            "shop  web-6d4b75cb6d-abcde  app  1  200Mi",
            "shop  web-6d4b75cb6d-abcde  app  120m  200M",
            "shop  web-6d4b75cb6d-abcde  app  120m  200Mi  extra",
        )
        for line in lines:
            with self.subTest(line=line), self.assertRaises(UnexpectedResponse) as raised:
                top_table(top("kube-system agent-x2k4p agent 4m 40Mi", line))
            self.assertEqual(
                str(raised.exception),
                "unexpected answer: kubectl top gave a line that is not a container's CPU and memory",
            )


class PeaksTest(unittest.TestCase):
    """peaks and usage_of."""

    def test_the_peak_is_the_highest_of_the_samples(self) -> None:
        """Peak CPU and memory may come from different samples, and a container one sample misses keeps its peak."""
        merged = peaks([top_table(sample) for sample in SAMPLES])
        self.assertEqual(merged[WEB_APP], Peak(180, 230 * MEBIBYTE))
        self.assertEqual(merged[REPORT], Peak(950, 700 * MEBIBYTE))
        self.assertEqual(len(merged), 5)

    def test_usage_holds_the_peaks_when_a_sample_answered(self) -> None:
        """Samples that gave figures make the usage, and a failure before them gives no reason."""
        tables = [top_table(SAMPLES[0])]
        self.assertEqual(usage_of(tables, "metrics-server is not installed"), Usage(peaks(tables), None))

    def test_no_sample_answered_keeps_why(self) -> None:
        """When no sample answered, the usage has no peaks and the last failure's reason."""
        self.assertEqual(usage_of([], "metrics-server is not installed"), Usage({}, "metrics-server is not installed"))

    def test_samples_with_no_figures_are_named(self) -> None:
        """Samples that answered with no container give no peaks, and say metrics-server gave no data."""
        self.assertEqual(usage_of([{}, {}], None), Usage({}, NO_DATA))
        self.assertEqual(NO_DATA, "metrics-server returned no data")
