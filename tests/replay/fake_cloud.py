"""The fakes a run under test gets: a virtual clock, a captured console, a stop switch and a fake cloud."""

import io
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Mapping, NoReturn, Optional, Sequence

from tests.replay.recording import RunFacts
from tests.replay.replay import ReplayRunner
from tests.replay.scenarios import Scenario
from zoorik.console import Console
from zoorik.errors import ToolNotInstalled
from zoorik.system.clock import Clock
from zoorik.system.http import HttpClient
from zoorik.system.process import CommandRunner, Completed
from zoorik.system.prompt import Prompt
from zoorik.system.runtime import Runtime, StopHandler


class _Elapsed(threading.local):
    """The virtual seconds one thread has waited; each thread starts at 0."""

    def __init__(self) -> None:
        """Start at 0 in each thread."""
        super().__init__()
        self.seconds = 0.0


class VirtualClock:
    """A clock that moves only when the run waits, and in each thread apart, so replays are quick and repeatable."""

    def __init__(self, start: datetime) -> None:
        """Start every thread's time at start."""
        self._start = start
        self._elapsed = _Elapsed()

    def now(self) -> datetime:
        """Give the start plus this thread's waits."""
        return self._start + timedelta(seconds=self._elapsed.seconds)

    def monotonic(self) -> float:
        """Give this thread's waits, in seconds."""
        return self._elapsed.seconds

    def sleep(self, seconds: float) -> None:
        """Move this thread's time on by seconds, at once."""
        self._elapsed.seconds += seconds


class CapturingConsole(Console):
    """A console that keeps each part's lines apart, so a run of two threads gives the same golden every time."""

    def __init__(self) -> None:
        """Keep lines instead of writing them; there is no terminal, so no status line."""
        super().__init__(io.StringIO(), terminal=False)
        self.lines: Dict[str, List[str]] = {}
        self.notes: List[str] = []

    def note(self, text: str) -> None:
        """Keep a stop's note; the real console writes it to the terminal's file descriptor, which a test lacks."""
        self.notes.append(text)

    def _emit(self, part: str, text: str) -> None:
        """Keep the line under its part."""
        self.lines.setdefault(part, []).append(text)


class Stopped(BaseException):
    """Raised by the fake exit, so a stop ends the run under test and not the test process."""

    def __init__(self, code: int) -> None:
        """Carry the exit code."""
        super().__init__(code)
        self.code = code


def exit_at_once(code: int) -> NoReturn:
    """Stand in for os._exit: raise Stopped, which the run's handlers don't catch."""
    raise Stopped(code)


class StopSwitch:
    """Stands in for the signals: keeps the run's stop handler, and calls it when a test fires a signal."""

    def __init__(self) -> None:
        """Hold no handler yet."""
        self._handler: Optional[StopHandler] = None

    def install(self, handler: StopHandler) -> None:
        """Keep handler, as install_stop would connect it to the signals."""
        self._handler = handler

    def fire(self, signum: int) -> None:
        """Call the handler as the signal signum would."""
        if self._handler is None:
            raise AssertionError("the run installed no stop handler")
        self._handler(signum)


class FakeRun:
    """A run under test: its facts and temporary folder, and the fakes the test reads afterwards."""

    def __init__(self, facts: RunFacts, temp_root: str) -> None:
        """Start the virtual clock at the facts' start time."""
        self.facts = facts
        self.temp_root = temp_root
        self.clock = VirtualClock(facts.started)
        self.console = CapturingConsole()
        self.stop = StopSwitch()

    def runtime(self, runner: CommandRunner, http: HttpClient, prompt: Prompt) -> Runtime:
        """Build the Runtime the run under test gets, around runner, http and prompt."""
        return Runtime(
            runner=runner,
            http=http,
            clock=self.clock,
            prompt=prompt,
            console=self.console,
            env=self.facts.env,
            salt=self.facts.salt,
            windows=self.facts.windows,
            temp_root=self.temp_root,
            exit_now=exit_at_once,
            install_stop=self.stop.install,
        )


class FakeCloud:
    """A CommandRunner for a scenario: a program it lacks is not installed, and the rest answer from its calls."""

    def __init__(self, scenario: Scenario, temp_root: str, clock: Clock) -> None:
        """Answer as scenario says, with the run's temp folder at temp_root and its time on clock."""
        self._installed = scenario.installed
        self._answers = ReplayRunner(scenario.calls, temp_root, clock)
        self.ended = 0

    def run(self, argv: Sequence[str], timeout: float, overrides: Mapping[str, str]) -> Completed:
        """Answer argv from the scenario."""
        if argv[0] not in self._installed:
            raise ToolNotInstalled(f"{argv[0]} is not installed")
        return self._answers.run(argv, timeout, overrides)

    def end_children(self) -> None:
        """Count the run's ask to end its programs; a scenario starts none."""
        self.ended += 1

    def unused(self) -> List[str]:
        """Name the key of each of the scenario's calls the run never made."""
        return self._answers.unused()
