"""The Runtime: everything the run takes from the computer, in one value that tests replace."""

import os
import secrets
import signal
import sys
import tempfile
from dataclasses import dataclass
from types import FrameType
from typing import Callable, List, NoReturn, Optional

from zoorik.console import Console
from zoorik.system.clock import Clock, SystemClock
from zoorik.system.env import EnvFacts
from zoorik.system.http import HttpClient
from zoorik.system.process import CommandRunner, SubprocessRunner
from zoorik.system.prompt import Prompt, TerminalPrompt

StopHandler = Callable[[int], None]


@dataclass(frozen=True)
class Runtime:
    """What the run uses from outside its own code: real_runtime builds the real one, and tests build fakes."""

    runner: CommandRunner
    http: HttpClient
    clock: Clock
    prompt: Prompt
    console: Console
    env: EnvFacts
    salt: str
    windows: bool
    temp_root: str
    exit_now: Callable[[int], NoReturn]
    install_stop: Callable[[StopHandler], None]


def real_runtime(http: HttpClient) -> Runtime:
    """Build the Runtime of a real run: this computer's programs, clock, terminal and environment, and http."""
    windows = sys.platform == "win32"
    return Runtime(
        runner=SubprocessRunner(windows=windows),
        http=http,
        clock=SystemClock(),
        prompt=TerminalPrompt(sys.stdin, sys.stdout),
        console=Console(sys.stdout, terminal=sys.stdout.isatty()),
        env=EnvFacts.read(os.environ),
        salt=secrets.token_hex(8),
        windows=windows,
        temp_root=make_temp_root(),
        exit_now=os._exit,
        install_stop=install_stop,
    )


def make_temp_root() -> str:
    """Make the run's temporary folder; mkdtemp makes it mode 700 on POSIX, so only the user can open it."""
    return tempfile.mkdtemp(prefix="zoorik-")


def stop_signals() -> List[signal.Signals]:
    """Name the signals that stop the run: Ctrl-C, a terminate request and, off Windows, a closed terminal."""
    if sys.platform == "win32":
        return [signal.SIGINT, signal.SIGTERM]
    else:
        return [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]


def install_stop(handler: StopHandler) -> None:
    """Call handler with the signal's number when one of the stop signals arrives."""

    def on_signal(signum: int, frame: Optional[FrameType]) -> None:
        handler(signum)

    for signum in stop_signals():
        signal.signal(signum, on_signal)
