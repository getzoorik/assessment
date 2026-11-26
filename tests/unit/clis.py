"""Made-up az and aws answers for the cloud readers' tests, and a runner that keeps every call it is given."""

import json
import os
import tempfile
import unittest
from typing import Dict, List, Mapping, Sequence, Tuple, Union

from tests.replay.fake_cloud import VirtualClock
from tests.replay.recording import Call
from tests.replay.replay import ReplayRunner
from tests.replay.scenarios import FACTS, call
from zoorik.cloud.aws import AWS
from zoorik.cloud.azure import AZURE
from zoorik.cloud.cli import Host
from zoorik.errors import AssessmentError
from zoorik.system.process import Completed

# A price list page, or the failure its fetch raises.
Page = Union[Mapping[str, object], AssessmentError]


def az(*args: str) -> List[str]:
    """Give an az command as the reader runs it, with the options every az call ends with."""
    return ["az", *args, *AZURE.output]


def aws(*args: str) -> List[str]:
    """Give an aws command as the reader runs it, with the options every aws call ends with."""
    return ["aws", *args, *AWS.output]


def answer(argv: Sequence[str], value: object) -> Call:
    """Make a call that answers argv with value, written as JSON."""
    return call(argv, json.dumps(value))


def failing(argv: Sequence[str], stderr: str) -> Call:
    """Make a call that fails with stderr as its error output."""
    return call(argv, exit_code=1, stderr=stderr)


class WatchingRunner:
    """A CommandRunner that answers from made-up calls, and keeps each call's argv, timeout and environment."""

    def __init__(self, calls: Sequence[Call], temp_root: str, clock: VirtualClock) -> None:
        """Answer from calls, with the run's temporary folder at temp_root."""
        self._replay = ReplayRunner(calls, temp_root, clock)
        self.runs: List[Tuple[List[str], float, Dict[str, str]]] = []
        # The text of each @file argument, read while the call runs: the reader removes the file afterwards.
        self.arg_files: Dict[str, str] = {}

    def run(self, argv: Sequence[str], timeout: float, overrides: Mapping[str, str]) -> Completed:
        """Keep the call, then give its made-up answer."""
        self.runs.append((list(argv), timeout, dict(overrides)))
        for arg in argv:
            if arg.startswith("@") and os.path.exists(arg[1:]):
                with open(arg[1:], encoding="utf-8", newline="") as file:
                    self.arg_files[arg] = file.read()
        return self._replay.run(argv, timeout, overrides)

    def end_children(self) -> None:
        """End nothing: a made-up call starts no program."""

    @property
    def argvs(self) -> List[List[str]]:
        """Give the argv of each call, in order."""
        return [argv for argv, _, _ in self.runs]


class PriceServer:
    """An HttpClient that answers each URL from a table, and keeps the URLs asked for."""

    def __init__(self, pages: Mapping[str, Page]) -> None:
        """Answer from pages: a page, or a failure to raise."""
        self._pages = pages
        self.urls: List[str] = []

    def get_json(self, url: str) -> object:
        """Give url's page, or raise its failure."""
        self.urls.append(url)
        page = self._pages[url]
        if isinstance(page, AssessmentError):
            raise type(page)(str(page))
        return page


class CloudTestCase(unittest.TestCase):
    """A test with a temporary folder and a virtual clock, whose hosts answer from made-up calls."""

    def setUp(self) -> None:
        """Make the run's temporary folder and its clock."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.temp_root = folder.name
        self.clock = VirtualClock(FACTS.started)

    def host(self, calls: Sequence[Call], *, windows: bool = False) -> Tuple[Host, WatchingRunner]:
        """Make a host whose runner answers from calls, and give the runner too."""
        runner = WatchingRunner(calls, self.temp_root, self.clock)
        return Host(runner, self.clock, self.temp_root, windows), runner
