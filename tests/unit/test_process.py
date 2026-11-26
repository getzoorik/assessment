"""The command runner, on real child processes of this Python."""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import List
from unittest import mock

from zoorik.errors import CommandTimedOut, ToolNotInstalled
from zoorik.system.process import NEW_PROCESS_GROUP, Completed, SubprocessRunner, creation_flags, output_codec

WINDOWS = sys.platform == "win32"


def python(code: str) -> List[str]:
    """Give the argv that runs code in this Python."""
    return [sys.executable, "-c", code]


def wait_for(path: str) -> None:
    """Wait until path exists, for at most 30 s."""
    deadline = time.monotonic() + 30
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.02)


class SubprocessRunnerTest(unittest.TestCase):
    """SubprocessRunner."""

    def test_a_missing_program_is_not_installed(self) -> None:
        """A program that is not on PATH raises ToolNotInstalled, naming it."""
        with self.assertRaisesRegex(ToolNotInstalled, "^zoorik-no-such-tool is not installed$"):
            SubprocessRunner(windows=WINDOWS).run(["zoorik-no-such-tool", "--version"], 30, {})

    def test_the_program_is_looked_up_on_the_path_it_gets(self) -> None:
        """The lookup uses the PATH the child would get, overrides included."""
        with tempfile.TemporaryDirectory() as empty, self.assertRaises(ToolNotInstalled):
            SubprocessRunner(windows=WINDOWS).run([os.path.basename(sys.executable), "-V"], 30, {"PATH": empty})

    def test_a_child_gets_the_users_environment_and_the_overrides(self) -> None:
        """The child gets the environment as it is when the call starts, with the caller's overrides on top."""
        runner = SubprocessRunner(windows=WINDOWS)
        code = "import os; print(os.environ['ZOORIK_TEST_USER'], os.environ['ZOORIK_TEST_OVERRIDE'])"
        user = {"ZOORIK_TEST_USER": "from the user", "ZOORIK_TEST_OVERRIDE": "from the user"}
        with mock.patch.dict(os.environ, user):
            completed = runner.run(python(code), 60, {"ZOORIK_TEST_OVERRIDE": "from the caller"})
        self.assertEqual(completed, Completed(0, "from the user from the caller\n", ""))

    def test_the_exit_code_and_both_outputs_come_back(self) -> None:
        """A failing program is not an error here: its exit code and output come back, as plain-line-end text."""
        code = "import sys; sys.stdout.buffer.write(b'out\\r\\n\\xff'); sys.stderr.write('err'); sys.exit(3)"
        completed = SubprocessRunner(windows=WINDOWS).run(python(code), 60, {})
        self.assertEqual(completed, Completed(3, "out\n�", "err"))

    def test_a_timeout_ends_the_child_and_raises(self) -> None:
        """A program that gives no answer in time is ended, and the call raises CommandTimedOut."""
        started = time.monotonic()
        with self.assertRaisesRegex(CommandTimedOut, "^no answer from .+ within 0.5 s$"):
            SubprocessRunner(windows=WINDOWS).run(python("import time; time.sleep(60)"), 0.5, {})
        self.assertLess(time.monotonic() - started, 30)

    def test_the_stop_ends_a_live_child(self) -> None:
        """end_children ends a program that is running, and its call returns at once."""
        runner = SubprocessRunner(windows=WINDOWS)
        results: List[Completed] = []
        with tempfile.TemporaryDirectory() as folder:
            started = os.path.join(folder, "started")
            code = f"import pathlib, time; pathlib.Path({started!r}).touch(); time.sleep(60)"
            caller = threading.Thread(target=lambda: results.append(runner.run(python(code), 120, {})))
            caller.start()
            wait_for(started)
            runner.end_children()
            caller.join(30)
        self.assertFalse(caller.is_alive())
        self.assertNotEqual(results[0].exit, 0)

    def test_a_child_started_after_the_stop_is_ended_at_once(self) -> None:
        """A program that starts while the run stops does not outlive it."""
        runner = SubprocessRunner(windows=WINDOWS)
        runner.end_children()
        started = time.monotonic()
        completed = runner.run(python("import time; time.sleep(60)"), 120, {})
        self.assertNotEqual(completed.exit, 0)
        self.assertLess(time.monotonic() - started, 30)


class WindowsChoicesTest(unittest.TestCase):
    """The Windows choices, as functions of the run's Windows flag, on every system."""

    def test_the_codec_table(self) -> None:
        """Only az on Windows writes the ANSI code page; every other case is UTF-8."""
        self.assertEqual(output_codec("az", windows=True), "mbcs")
        for tool in ("aws", "kubectl", "kubelogin"):
            self.assertEqual(output_codec(tool, windows=True), "utf-8")
        for tool in ("az", "aws", "kubectl", "kubelogin"):
            self.assertEqual(output_codec(tool, windows=False), "utf-8")

    def test_windows_starts_each_child_in_its_own_process_group(self) -> None:
        """With the flag on, children start in their own process group; with it off, as usual."""
        self.assertEqual(creation_flags(windows=True), NEW_PROCESS_GROUP)
        self.assertEqual(creation_flags(windows=False), 0)

    @unittest.skipUnless(sys.platform == "win32", "the constant exists only on Windows")
    def test_the_process_group_flag_is_windows_own(self) -> None:
        """NEW_PROCESS_GROUP is the value of subprocess.CREATE_NEW_PROCESS_GROUP."""
        if sys.platform == "win32":
            self.assertEqual(NEW_PROCESS_GROUP, subprocess.CREATE_NEW_PROCESS_GROUP)
