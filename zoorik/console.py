"""The run's terminal: whole lines from any thread, one shared status line, and lines held during a question."""

import threading
from contextlib import contextmanager
from typing import Callable, ContextManager, Dict, Iterator, List, TextIO, Tuple

STATUS_SEPARATOR = " · "
# The part name the run's own lines carry; the parts are named in capitals, such as Kubernetes and Disks.
RUN = "run"

# Progress of a read, for the status line: done and total.
Progress = Callable[[int, int], None]


class Console:
    """Writes whole lines under one lock, so lines from different threads never mix.

    On a terminal one status line sits below the lines and is redrawn as the parts report progress; elsewhere, such as
    a pipe, a file or CI, it is never drawn. While a question is open, every other line waits, then follows the answer.
    """

    def __init__(self, out: TextIO, *, terminal: bool) -> None:
        """Write to out, drawing the status line only when out is a terminal."""
        self._out = out
        self._terminal = terminal
        self._lock = threading.Lock()
        self._parts: List[str] = []
        self._progress: Dict[str, str] = {}
        self._drawn = 0
        self._asking = False
        self._held: List[Tuple[str, str]] = []

    def part(self, name: str) -> "ConsolePart":
        """Give a part of the run its own lines and its place on the status line, in the order the parts are made."""
        with self._lock:
            self._parts.append(name)
        return ConsolePart(self, name)

    def line(self, text: str = "") -> None:
        """Write one of the run's own lines, such as the start line or the report's path."""
        self._line(RUN, text)

    def clear_status(self) -> None:
        """Take the status line away, before the closing lines."""
        with self._lock:
            self._erase_status()
            self._progress.clear()

    @contextmanager
    def question(self) -> Iterator[None]:
        """Hold every other line, and the status line, while the caller asks on the terminal."""
        with self._lock:
            self._erase_status()
            self._asking = True
        try:
            yield
        finally:
            with self._lock:
                self._asking = False
                held, self._held = self._held, []
                for part, text in held:
                    self._emit(part, text)
                self._draw_status()

    def _line(self, part: str, text: str) -> None:
        """Write one whole line for a part, or hold it while a question is open."""
        with self._lock:
            if self._asking:
                self._held.append((part, text))
                return
            self._erase_status()
            self._emit(part, text)
            self._draw_status()

    def _status(self, part: str, text: str) -> None:
        """Set a part's progress on the status line."""
        with self._lock:
            self._progress[part] = text
            if not self._asking:
                self._draw_status()

    def _emit(self, part: str, text: str) -> None:
        """Write one line; the lock is held. The part isn't printed: a test's console overrides this to keep it."""
        self._out.write(text + "\n")
        self._out.flush()

    def _draw_status(self) -> None:
        """Draw the status line below the lines, on a terminal only; the lock is held."""
        if not self._terminal:
            return
        text = STATUS_SEPARATOR.join(f"{name} {self._progress[name]}" for name in self._parts if name in self._progress)
        if not text and not self._drawn:
            return
        # Back to the line's start; the padding covers what a longer status left behind.
        self._out.write("\r" + text.ljust(self._drawn))
        self._out.flush()
        self._drawn = len(text)

    def _erase_status(self) -> None:
        """Blank the status line, so a line or a question can take its place; the lock is held."""
        if self._drawn:
            self._out.write("\r" + " " * self._drawn + "\r")
            self._out.flush()
            self._drawn = 0


class ConsolePart:
    """One part's handle on the console: its lines, its progress on the status line and its questions."""

    def __init__(self, console: Console, name: str) -> None:
        """Speak for the part called name on console."""
        self._console = console
        self._name = name

    def line(self, text: str = "") -> None:
        """Write one of this part's lines."""
        self._console._line(self._name, text)

    def status(self, text: str) -> None:
        """Set this part's progress on the status line, such as 12/40 VMs."""
        self._console._status(self._name, text)

    def progress(self, noun: str) -> Progress:
        """Give the progress of a read of nouns, which sets this part's status line to done/total nouns."""

        def show(done: int, total: int) -> None:
            self.status(f"{done}/{total} {noun}")

        return show

    def question(self) -> ContextManager[None]:
        """Hold every other line while this part asks on the terminal."""
        return self._console.question()
