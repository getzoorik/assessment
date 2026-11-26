"""Kubernetes quantities, read exactly and counted as the scheduler counts them."""

import unittest
from fractions import Fraction

from zoorik.errors import UnexpectedResponse
from zoorik.kubectl.quantity import quantity, resources, scheduler_units


class QuantityTest(unittest.TestCase):
    """quantity."""

    def test_each_form_the_api_writes_is_read_exactly(self) -> None:
        """Plain numbers, decimal and binary suffixes, and decimal exponents give exact values."""
        cases = {
            "4": Fraction(4),
            "0.5": Fraction(1, 2),
            ".5": Fraction(1, 2),
            "1.": Fraction(1),
            "+2": Fraction(2),
            "250m": Fraction(1, 4),
            "100u": Fraction(1, 10**4),
            "500n": Fraction(1, 2 * 10**6),
            "1k": Fraction(1000),
            "1.5Gi": Fraction(3 * 2**29),
            "64Mi": Fraction(64 * 2**20),
            "16393364Ki": Fraction(16393364 * 1024),
            "2E": Fraction(2 * 10**18),
            "1e3": Fraction(1000),
            "1E3": Fraction(1000),
            "12e-1": Fraction(6, 5),
        }
        for text, value in cases.items():
            with self.subTest(text=text):
                self.assertEqual(quantity(text), value)

    def test_any_other_text_is_unexpected(self) -> None:
        """Text that is not a quantity is refused, and the refusal names it."""
        for text in ("", "Gi", "1.5.3", "1Kb", "1 Gi", "one", "1i", "0x10"):
            with self.subTest(text=text), self.assertRaisesRegex(UnexpectedResponse, "is not a Kubernetes quantity"):
                quantity(text)


class SchedulerUnitsTest(unittest.TestCase):
    """scheduler_units and resources."""

    def test_cpu_is_counted_in_millicores_rounded_up(self) -> None:
        """CPU becomes millicores, and a part of a millicore counts as a whole one."""
        self.assertEqual(scheduler_units("cpu", quantity("250m")), 250)
        self.assertEqual(scheduler_units("cpu", quantity("0.1")), 100)
        self.assertEqual(scheduler_units("cpu", quantity("3860m")), 3860)
        self.assertEqual(scheduler_units("cpu", quantity("100u")), 1)

    def test_anything_else_is_counted_in_whole_units_rounded_up(self) -> None:
        """Memory, storage and counts become whole units, and a part of one counts as a whole one."""
        self.assertEqual(scheduler_units("memory", quantity("1.5Ki")), 1536)
        self.assertEqual(scheduler_units("memory", quantity("0.5")), 1)
        self.assertEqual(scheduler_units("pods", quantity("110")), 110)
        self.assertEqual(scheduler_units("memory", quantity("129M")), 129 * 10**6)

    def test_a_resource_list_is_read_in_scheduler_units(self) -> None:
        """Each resource of a list is read in its own unit."""
        self.assertEqual(
            resources({"cpu": "3860m", "memory": "12880532Ki", "pods": "110", "ephemeral-storage": "119703055367"}),
            {"cpu": 3860, "memory": 12880532 * 1024, "pods": 110, "ephemeral-storage": 119703055367},
        )
        self.assertEqual(resources({}), {})
