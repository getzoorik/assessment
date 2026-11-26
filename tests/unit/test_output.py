"""The report's folder and file."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

from zoorik.errors import ReportNotWritable
from zoorik.output import report_folder, write_report

WRITTEN = datetime(2026, 3, 4, 5, 6, 59, tzinfo=timezone.utc)


def running_as_root() -> bool:
    """Tell whether the tests run as root, who may write in any folder whatever its mode."""
    if sys.platform == "win32":
        return False
    else:
        return os.geteuid() == 0


class OutputTest(unittest.TestCase):
    """report_folder and write_report, in a new temporary folder."""

    def setUp(self) -> None:
        """Make the temporary folder."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = temp.name

    def test_out_makes_the_folder(self) -> None:
        """A folder that doesn't exist yet is made, with its parents, and the probe file leaves nothing behind."""
        out = os.path.join(self.temp, "reports", "today")
        self.assertEqual(report_folder(out), os.path.abspath(out))
        self.assertEqual(os.listdir(out), [])

    def test_without_out_the_report_goes_in_the_current_folder(self) -> None:
        """With no --out, the folder is the current one."""
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.temp)
        self.assertEqual(report_folder(None), os.getcwd())

    def test_a_file_is_not_a_folder(self) -> None:
        """--out naming a file stops the run, and so does a folder under a file."""
        path = os.path.join(self.temp, "notes.txt")
        with open(path, "w", encoding="utf-8") as notes:
            notes.write("notes")
        with self.assertRaisesRegex(ReportNotWritable, r"notes\.txt is a file; --out takes a folder"):
            report_folder(path)
        with self.assertRaisesRegex(ReportNotWritable, "^cannot write to "):
            report_folder(os.path.join(path, "reports"))

    @unittest.skipIf(sys.platform == "win32" or running_as_root(), "the folder's mode bars writing only off Windows")
    def test_a_folder_that_cannot_be_written_is_refused(self) -> None:
        """A folder the user may not write in raises ReportNotWritable, found by the probe file."""
        folder = os.path.join(self.temp, "read-only")
        os.mkdir(folder, 0o500)
        self.addCleanup(os.chmod, folder, 0o700)
        with self.assertRaisesRegex(ReportNotWritable, "^cannot write to .*read-only: Permission denied$"):
            report_folder(folder)

    def test_the_name_carries_the_minute_in_utc(self) -> None:
        """The report is zoorik-assessment-YYYYMMDD-HHMM.pdf, with what was written."""
        path = write_report(self.temp, b"%PDF-1.4 one", WRITTEN)
        self.assertEqual(path, os.path.join(self.temp, "zoorik-assessment-20260304-0506.pdf"))
        with open(path, "rb") as report:
            self.assertEqual(report.read(), b"%PDF-1.4 one")

    def test_reports_in_one_minute_never_overwrite(self) -> None:
        """A name that is taken gets -2, then -3, and every report keeps its own content."""
        paths = [write_report(self.temp, f"report {number}".encode(), WRITTEN) for number in (1, 2, 3)]
        self.assertEqual(
            [os.path.basename(path) for path in paths],
            [
                "zoorik-assessment-20260304-0506.pdf",
                "zoorik-assessment-20260304-0506-2.pdf",
                "zoorik-assessment-20260304-0506-3.pdf",
            ],
        )
        for number, path in zip((1, 2, 3), paths):
            with open(path, "rb") as report:
                self.assertEqual(report.read(), f"report {number}".encode())

    def test_a_folder_gone_before_writing_is_refused(self) -> None:
        """If the folder disappears after the check, the write raises ReportNotWritable."""
        with self.assertRaisesRegex(ReportNotWritable, "^cannot write "):
            write_report(os.path.join(self.temp, "gone"), b"%PDF-1.4", WRITTEN)
