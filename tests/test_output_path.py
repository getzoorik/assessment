"""Regression tests for k8s.py report naming (issue #6).

A second run in the same minute must not overwrite the first report, while a
file the user names with --out keeps the exact name they chose.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("k8s", os.path.join(ROOT, "k8s.py"))
k8s = importlib.util.module_from_spec(_spec)
sys.modules["k8s"] = k8s  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(k8s)

STEM = "zoorik-k8s-assessment-20260102-0304"


class FrozenClock:
    """A datetime.now that always answers one instant, so two calls share a minute."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class OutputPathTest(unittest.TestCase):
    def setUp(self):
        self._datetime = k8s.datetime
        k8s.datetime = FrozenClock
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self._expanduser = k8s.os.path.expanduser
        k8s.os.path.expanduser = lambda path: self.dir if path == "~" else self._expanduser(path)

    def tearDown(self):
        k8s.datetime = self._datetime
        k8s.os.path.expanduser = self._expanduser
        self._tmp.cleanup()

    @staticmethod
    def touch(path):
        with open(path, "w", encoding="utf-8"):
            pass

    def test_second_run_in_the_same_minute_does_not_overwrite(self):
        folder = self.dir + os.sep
        first = k8s.output_path(folder, "")
        self.assertEqual(os.path.dirname(first), os.path.abspath(self.dir))
        self.assertEqual(os.path.basename(first), STEM + ".xlsx")
        self.touch(first)
        second = k8s.output_path(folder, "")
        self.assertNotEqual(first, second)
        self.assertEqual(os.path.basename(second), STEM + "-2.xlsx")
        self.touch(second)
        third = k8s.output_path(folder, "")
        self.assertEqual(os.path.basename(third), STEM + "-3.xlsx")

    def test_run_without_out_does_not_overwrite(self):
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            first = k8s.output_path(None, "")
            self.touch(first)
            second = k8s.output_path(None, "")
        finally:
            os.chdir(old)
        self.assertNotEqual(first, second)
        self.assertEqual(os.path.dirname(second), os.path.abspath(self.dir))
        self.assertTrue(second.endswith("-2.xlsx"), second)

    def test_existing_folder_is_found_without_a_separator(self):
        first = k8s.output_path(self.dir, "")
        self.assertEqual(os.path.basename(first), STEM + ".xlsx")
        self.touch(first)
        second = k8s.output_path(self.dir, "")
        self.assertEqual(os.path.basename(second), STEM + "-2.xlsx")

    def test_a_new_folder_is_created_and_keeps_the_plain_name(self):
        fresh = os.path.join(self.dir, "fresh")
        path = k8s.output_path(fresh + os.sep, "")
        self.assertTrue(os.path.isdir(fresh))
        self.assertEqual(os.path.basename(path), STEM + ".xlsx")

    def test_a_file_the_user_names_is_kept_exactly(self):
        chosen = os.path.join(self.dir, "mine.xlsx")
        self.touch(chosen)
        self.assertEqual(k8s.output_path(chosen, ""), os.path.abspath(chosen))
        report = os.path.join(self.dir, "report")
        self.touch(report + ".xlsx")
        self.assertEqual(k8s.output_path(report, ""), os.path.abspath(report + ".xlsx"))

    def test_cloud_shell_folder_keeps_the_report(self):
        first = k8s.output_path(None, "azure")
        self.assertTrue(first.endswith(STEM + ".xlsx"), first)
        self.touch(first)
        second = k8s.output_path(None, "azure")
        self.assertNotEqual(first, second)
        self.assertTrue(second.endswith(STEM + "-2.xlsx"), second)


if __name__ == "__main__":
    unittest.main()
