"""The one way az and aws run: output options, the call's environment, one throttle retry and typed failures."""

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import List, Mapping, Optional, Sequence, Tuple, Type

from zoorik.cloud.fields import parse_json
from zoorik.errors import AssessmentError, CommandFailed, Throttled
from zoorik.system.clock import Clock
from zoorik.system.process import CommandRunner, Completed

# A throttled call is tried 3 times: it waits 5 s after the first try and 10 s after the second.
THROTTLE_WAITS = (5.0, 10.0)
DEFAULT_TIMEOUT = 120.0
# The characters cmd.exe reads again in az.cmd's arguments; on Windows a value holding one goes to az in a file.
CMD_SPECIAL = re.compile(r'["&|<>^%()\r\n]')
# A reason taken from a CLI's own error line is shortened to this length, so it fits one console line.
REASON_LENGTH = 100
ERROR_PREFIX = re.compile(r"^error[:\s]+", re.IGNORECASE)
# Quoted parts of an error line name the user's own resources, so the reason leaves them out.
QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")


@dataclass(frozen=True)
class Reason:
    """One row of a reasons table: a pattern in a CLI's error output, the kind of failure, and the reader's text."""

    pattern: str
    kind: Type[AssessmentError]
    text: str


@dataclass(frozen=True)
class Tool:
    """How one cloud's CLI is called."""

    cloud: str
    # The options every call ends with: JSON output, and on az no warnings.
    output: Tuple[str, ...]
    # Put on top of the user's environment for each call only; the user's own settings stay as they are.
    overrides: Mapping[str, str]
    reasons: Tuple[Reason, ...]
    # On Windows az is az.cmd, whose arguments cmd.exe reads again.
    cmd_script: bool


@dataclass(frozen=True)
class Host:
    """What the cloud CLIs take from the run: its runner, clock and temporary folder, and its Windows flag."""

    runner: CommandRunner
    clock: Clock
    temp_root: str
    windows: bool


@dataclass(frozen=True)
class Window:
    """A span of time a metric is read for, in UTC."""

    start: datetime
    end: datetime


def utc_text(moment: datetime) -> str:
    """Write a UTC time as the clouds' APIs take it, such as 2026-01-05T09:00:00Z."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def kubeconfig_file(temp_root: str, identity: Sequence[str]) -> str:
    """Make the folder for the run's kubeconfigs and name one cluster's file from its identity.

    The name is a hash of the identity, so it is the same on every run and a replay finds the call it recorded.
    """
    folder = os.path.join(temp_root, "kube")
    os.makedirs(folder, exist_ok=True)
    digest = hashlib.sha256("\n".join(identity).encode("utf-8")).hexdigest()[:16]
    return os.path.join(folder, f"{digest}.yaml")


def summary(output: str) -> str:
    """Make a short reason from a CLI's error output that no table row names: its first line, without names."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "unknown error"
    return QUOTED.sub("...", ERROR_PREFIX.sub("", lines[0]))[:REASON_LENGTH]


class CloudCli:
    """One cloud's CLI: every call ends with the output options and runs with the call's environment overrides.

    A throttled call is tried again, and a failed one becomes the typed failure its reasons table names.
    """

    def __init__(self, tool: Tool, host: Host) -> None:
        """Run tool's calls on host's runner, waiting on host's clock."""
        self.tool = tool
        self._runner = host.runner
        self._clock = host.clock
        self._arg_folder = host.temp_root if host.windows and tool.cmd_script else None

    def read(self, argv: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> object:
        """Run argv, the program and its arguments, and give its JSON answer."""
        completed = self._call(argv, timeout)
        if completed.exit != 0:
            raise self.failure(completed.stderr)
        return parse_json(completed.stdout, argv[0])

    def read_unless(self, argv: Sequence[str], absent: Reason, timeout: float = DEFAULT_TIMEOUT) -> Optional[object]:
        """Run argv and give its JSON answer, or None when it fails with the reason absent."""
        completed = self._call(argv, timeout)
        if completed.exit != 0:
            if self._row(completed.stderr) is absent:
                return None
            raise self.failure(completed.stderr)
        return parse_json(completed.stdout, argv[0])

    def run(self, argv: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> None:
        """Run argv, which answers with no JSON, to its end."""
        completed = self._call(argv, timeout)
        if completed.exit != 0:
            raise self.failure(completed.stderr)

    def failure(self, text: str) -> AssessmentError:
        """Give the typed failure for a CLI's error text: its reasons table's row, else a short reason."""
        row = self._row(text)
        return CommandFailed(summary(text)) if row is None else row.kind(row.text)

    def _call(self, argv: Sequence[str], timeout: float) -> Completed:
        """Run argv, trying again while the cloud throttles it, and give the last answer."""
        files: List[str] = []
        try:
            command = [*self._arguments(argv, files), *self.tool.output]
            for wait in THROTTLE_WAITS:
                completed = self._runner.run(command, timeout, self.tool.overrides)
                if not self._throttled(completed):
                    return completed
                self._clock.sleep(wait)
            return self._runner.run(command, timeout, self.tool.overrides)
        finally:
            for path in files:
                os.remove(path)

    def _throttled(self, completed: Completed) -> bool:
        """Tell whether a call failed because the cloud throttled it."""
        if completed.exit == 0:
            return False
        row = self._row(completed.stderr)
        return row is not None and row.kind is Throttled

    def _row(self, text: str) -> Optional[Reason]:
        """Find the first row of the reasons table whose pattern is in text."""
        return next((row for row in self.tool.reasons if re.search(row.pattern, text, re.IGNORECASE)), None)

    def _arguments(self, argv: Sequence[str], files: List[str]) -> List[str]:
        """Give argv as the program gets it: on Windows, az values that cmd.exe would change go in files."""
        if self._arg_folder is None:
            return list(argv)
        return [argv[0], *(self._arg_file(arg, files) if CMD_SPECIAL.search(arg) else arg for arg in argv[1:])]

    def _arg_file(self, value: str, files: List[str]) -> str:
        """Write value to a file in the run's temporary folder and give it as az's @file argument."""
        handle, path = tempfile.mkstemp(prefix="arg-", suffix=".txt", dir=self._arg_folder)
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as file:
            file.write(value)
        files.append(path)
        return "@" + path
