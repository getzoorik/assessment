"""The shared number formats."""

import unittest

from zoorik.format import count, half_up, usd


class FormatTest(unittest.TestCase):
    """half_up, usd and count."""

    def test_half_up_sends_halves_up(self) -> None:
        """Halves go up, where round() would send them to the even number."""
        self.assertEqual([half_up(value) for value in (0.49, 0.5, 1.5, 2.5, 3.5, 1234.5)], [0, 1, 2, 3, 4, 1235])

    def test_half_up_sends_a_half_up_where_the_float_sits_just_below_it(self) -> None:
        """730 hours at $0.35 is $255.50, which floats hold as 255.49999999999997; it still goes up to $256."""
        self.assertEqual(half_up(730 * 0.35), 256)

    def test_usd_writes_whole_dollars_with_separators(self) -> None:
        """Dollars come with a dollar sign and thousands separators."""
        self.assertEqual([usd(0), usd(999), usd(1234), usd(1234567)], ["$0", "$999", "$1,234", "$1,234,567"])

    def test_count_makes_the_noun_plural_unless_one(self) -> None:
        """One takes the singular; every other number takes the plural, with thousands separators."""
        self.assertEqual(
            [count(0, "machine"), count(1, "machine"), count(2, "machine"), count(1200, "machine")],
            ["0 machines", "1 machine", "2 machines", "1,200 machines"],
        )
