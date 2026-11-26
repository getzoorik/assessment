"""The Runtime and the real one."""

import os
import signal
import stat
import sys
import unittest
from typing import List

from zoorik.console import Console
from zoorik.system.clock import SystemClock
from zoorik.system.env import EnvFacts
from zoorik.system.process import SubprocessRunner
from zoorik.system.prompt import TerminalPrompt
from zoorik.system.runtime import install_stop, make_temp_root, real_runtime, stop_signals


class NoHttp:
    """An HTTP client that fails any fetch, for tests that must not fetch."""

    def get_json(self, url: str) -> object:
        """Fail the test."""
        raise AssertionError(f"fetched {url}")


class RuntimeTest(unittest.TestCase):
    """EnvFacts, the stop signals and real_runtime."""

    def test_env_facts_keep_only_the_three_facts(self) -> None:
        """Only the Cloud Shell variables are kept; every other variable is left out."""
        environ = {"ACC_CLOUD": "PROD", "AWS_EXECUTION_ENV": "CloudShell", "HOME": "/home/someone", "TOKEN": "x"}
        self.assertEqual(EnvFacts.read(environ), EnvFacts("PROD", None, "CloudShell"))

    def test_the_stop_signals(self) -> None:
        """Ctrl-C and a terminate request stop the run everywhere, and a closed terminal does off Windows."""
        if sys.platform == "win32":
            self.assertEqual(stop_signals(), [signal.SIGINT, signal.SIGTERM])
        else:
            self.assertEqual(stop_signals(), [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])

    def test_install_stop_calls_the_handler_with_the_signal(self) -> None:
        """A stop signal reaches the handler as its number."""
        for signum in stop_signals():
            self.addCleanup(signal.signal, signum, signal.getsignal(signum))
        received: List[int] = []
        install_stop(received.append)
        signal.raise_signal(signal.SIGTERM)
        self.assertEqual(received, [signal.SIGTERM])

    def test_the_real_runtime(self) -> None:
        """The real runtime runs this computer's programs, clock, terminal and environment, with a new salt."""
        runtime = real_runtime(NoHttp())
        self.addCleanup(os.rmdir, runtime.temp_root)
        self.assertIsInstance(runtime.runner, SubprocessRunner)
        self.assertIsInstance(runtime.http, NoHttp)
        self.assertIsInstance(runtime.clock, SystemClock)
        self.assertIsInstance(runtime.prompt, TerminalPrompt)
        self.assertIsInstance(runtime.console, Console)
        self.assertEqual(runtime.env, EnvFacts.read(os.environ))
        self.assertRegex(runtime.salt, "^[0-9a-f]{16}$")
        self.assertIs(runtime.windows, sys.platform == "win32")
        self.assertTrue(os.path.isdir(runtime.temp_root))
        self.assertIs(runtime.exit_now, os._exit)
        self.assertIs(runtime.install_stop, install_stop)

    @unittest.skipUnless(os.name == "posix", "file modes are POSIX")
    def test_the_temp_root_is_the_users_alone(self) -> None:
        """The temporary folder is mode 700, as it holds kubeconfigs."""
        root = make_temp_root()
        self.addCleanup(os.rmdir, root)
        self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o700)
