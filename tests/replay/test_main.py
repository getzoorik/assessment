"""python -m tests.replay: its checks run before anything else."""

import os
import tempfile
import unittest
from unittest import mock

from tests.replay.__main__ import main, split
from tests.replay.recording import REPO, RecordingRefused


class RecordCommandTest(unittest.TestCase):
    """The record command; the run it would start is replaced by a failure, so no test reaches a cloud."""

    def setUp(self) -> None:
        """Make any start of a real run fail the test."""
        real_run = mock.patch("tests.replay.__main__.run_for_real", side_effect=AssertionError("a real run started"))
        real_run.start()
        self.addCleanup(real_run.stop)

    def test_a_path_inside_the_repository_is_refused_first(self) -> None:
        """A recording path inside the repository stops the command before the run starts."""
        with self.assertRaises(RecordingRefused):
            main(["record", "--out", os.path.join(REPO, "run.json.gz"), "--", "zoorik", "assess"])

    def test_an_existing_file_is_refused_first(self) -> None:
        """A recording path that exists stops the command before the run starts."""
        with tempfile.NamedTemporaryFile() as existing, self.assertRaisesRegex(SystemExit, "never replaces a file"):
            main(["record", "--out", existing.name, "--", "zoorik", "assess"])

    def test_only_zoorik_assess_is_recorded(self) -> None:
        """The command after -- must be zoorik assess."""
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(SystemExit, "zoorik assess"):
            main(["record", "--out", os.path.join(folder, "run.json.gz"), "--", "python", "-c", "pass"])

    def test_the_arguments_split_at_the_separator(self) -> None:
        """Everything after -- is the command to record."""
        self.assertEqual(
            split(["record", "--out", "run.json.gz", "--", "zoorik", "assess", "--anonymize"]),
            (["record", "--out", "run.json.gz"], ["zoorik", "assess", "--anonymize"]),
        )
        self.assertEqual(split(["update"]), (["update"], []))
