"""The failure types."""

import unittest

from zoorik.errors import AssessmentError


class ErrorsTest(unittest.TestCase):
    """One type per kind of failure."""

    def test_each_kind_is_its_own_type(self) -> None:
        """The run tells failures apart by these types, never by their text."""
        self.assertEqual(
            sorted(kind.__name__ for kind in AssessmentError.__subclasses__()),
            [
                "AccessDenied",
                "ClusterUnreadable",
                "CommandFailed",
                "CommandTimedOut",
                "NotSignedIn",
                "PriceListUnreachable",
                "PriceNotFound",
                "ReportNotWritable",
                "Throttled",
                "ToolNotInstalled",
                "UnexpectedResponse",
            ],
        )

    def test_the_text_is_the_reason(self) -> None:
        """A failure's text is exactly the reason it was given, which is what the reader sees."""
        for kind in AssessmentError.__subclasses__():
            with self.subTest(kind=kind.__name__):
                self.assertEqual(str(kind("access denied")), "access denied")
