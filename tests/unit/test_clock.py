"""The system clock."""

import unittest
from datetime import timedelta

from zoorik.system.clock import SystemClock


class SystemClockTest(unittest.TestCase):
    """SystemClock."""

    def test_now_is_in_utc(self) -> None:
        """The time of day carries the UTC offset, so report names are in UTC."""
        self.assertEqual(SystemClock().now().utcoffset(), timedelta(0))

    def test_the_steady_clock_moves_across_a_sleep(self) -> None:
        """The steady clock moves forward while the clock sleeps."""
        clock = SystemClock()
        before = clock.monotonic()
        clock.sleep(0.05)
        self.assertGreater(clock.monotonic(), before)
