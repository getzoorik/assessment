"""The y/N question, asked only where someone can answer it."""

import sys
from typing import Optional, Protocol, TextIO

if sys.platform == "win32":
    import msvcrt
else:
    import termios


class Prompt(Protocol):
    """Asks the person at the terminal a y/N question."""

    def ask(self, question: str) -> Optional[bool]:
        """Give True for yes, False for any other answer, and None when there is no terminal to ask on."""


class TerminalPrompt:
    """Asks on the terminal the run was started from."""

    def __init__(self, stdin: TextIO, stdout: TextIO) -> None:
        """Write questions to stdout and read the answers from stdin."""
        self._stdin = stdin
        self._stdout = stdout

    def ask(self, question: str) -> Optional[bool]:
        """Ask question and read one line; None when stdin is not a terminal, as in CI or a pipe."""
        if not self._stdin.isatty():
            return None
        # Keys typed before the question appears must not answer it.
        flush_pending_keys(self._stdin)
        self._stdout.write(question)
        self._stdout.flush()
        answer = self._stdin.readline()
        if not answer.endswith("\n"):
            # The input ended without Enter, so the next line would start after the question.
            self._stdout.write("\n")
            self._stdout.flush()
        return answer.strip().lower() in ("y", "yes")


def flush_pending_keys(stdin: TextIO) -> None:
    """Drop the keys typed on the terminal that nothing has read yet."""
    if sys.platform == "win32":
        while msvcrt.kbhit():
            msvcrt.getwch()
    else:
        termios.tcflush(stdin.fileno(), termios.TCIFLUSH)
