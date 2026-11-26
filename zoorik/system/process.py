"""The one place the run starts programs, through a runner that keeps its live children so a stop can end them."""

import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Dict, List, Mapping, Protocol, Sequence, Set

from zoorik.errors import CommandTimedOut, ToolNotInstalled

# subprocess.CREATE_NEW_PROCESS_GROUP, which exists only on Windows; a Windows test checks the two are equal.
NEW_PROCESS_GROUP = 0x00000200


@dataclass(frozen=True)
class Completed:
    """What a program left when it exited: its exit code and its output as text."""

    exit: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Runs programs to the end, and ends the live ones when the run stops."""

    def run(self, argv: Sequence[str], timeout: float, overrides: Mapping[str, str]) -> Completed:
        """Run argv (the program's name, then its arguments) with overrides on top of the user's environment."""

    def end_children(self) -> None:
        """End every live program, and any program started from now on."""


def output_codec(tool: str, *, windows: bool) -> str:
    """Name the codec of a program's piped output: az on Windows writes the ANSI code page, all else UTF-8."""
    # az.cmd starts its Python with -I, so no setting moves az's piped output off the ANSI code page.
    return "mbcs" if windows and tool == "az" else "utf-8"


def creation_flags(*, windows: bool) -> int:
    """Give the flags each program starts with: on Windows its own process group, so Ctrl-C reaches only the run."""
    return NEW_PROCESS_GROUP if windows else 0


class SubprocessRunner:
    """Runs programs as child processes, and keeps the live ones."""

    def __init__(self, *, windows: bool) -> None:
        """Start programs as the platform needs; windows is the run's Windows flag."""
        self._windows = windows
        self._lock = threading.Lock()
        self._live: Set[subprocess.Popen[bytes]] = set()
        self._stopped = False

    def run(self, argv: Sequence[str], timeout: float, overrides: Mapping[str, str]) -> Completed:
        """Run argv within timeout seconds; the program gets the user's environment as it is now, plus overrides."""
        tool = argv[0]
        env = {**os.environ, **overrides}
        program = shutil.which(tool, path=env.get("PATH"))
        if program is None:
            raise ToolNotInstalled(f"{tool} is not installed")
        child = self._start([program, *argv[1:]], env)
        # Leaving the block closes the pipes and waits for the child, as subprocess.run does.
        with child:
            try:
                stdout, stderr = child.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                child.kill()
                if sys.platform == "win32":
                    # On Windows threads read the pipes; communicate() lets them finish before the block closes them.
                    child.communicate()
                raise CommandTimedOut(f"no answer from {tool} within {timeout:g} s") from None
            finally:
                with self._lock:
                    self._live.discard(child)
        codec = output_codec(tool, windows=self._windows)
        return Completed(child.returncode, _text(stdout, codec), _text(stderr, codec))

    def end_children(self) -> None:
        """End every live program, and any program started from now on."""
        with self._lock:
            self._stopped = True
            live = list(self._live)
        for child in live:
            child.kill()

    def _start(self, command: List[str], env: Dict[str, str]) -> "subprocess.Popen[bytes]":
        """Start one program and keep it as live."""
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=creation_flags(windows=self._windows),
        )
        with self._lock:
            self._live.add(child)
            stopped = self._stopped
        if stopped:
            # A program that starts while the run stops would outlive the run.
            child.kill()
        return child


def _text(data: bytes, codec: str) -> str:
    """Decode a program's output, with Windows line ends made plain."""
    return data.decode(codec, errors="replace").replace("\r\n", "\n")
