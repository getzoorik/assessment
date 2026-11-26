"""The console: whole lines from any thread, one status line both parts share, and lines held during a question."""

import io
import os
import tempfile
import threading
import time
import unittest
from typing import List

from zoorik.console import Console, ConsolePart

QUESTION = "Read disk use inside the VMs? [y/N] "


def screen(output: str) -> List[str]:
    """Give the lines a terminal shows for output, where a carriage return goes back to the start of the line."""
    lines: List[str] = []
    current: List[str] = []
    column = 0
    for char in output:
        if char == "\n":
            lines.append("".join(current).rstrip())
            current, column = [], 0
        elif char == "\r":
            column = 0
        else:
            if column < len(current):
                current[column] = char
            else:
                current.append(char)
            column += 1
    return [*lines, "".join(current).rstrip()]


class SlowStream(io.StringIO):
    """A stream that takes one character at a time and lets other threads run between them."""

    def write(self, text: str) -> int:
        """Write text a character at a time."""
        for char in text:
            super().write(char)
            time.sleep(0)
        return len(text)


def write_lines(part: ConsolePart, lines: List[str]) -> None:
    """Write each of lines as part."""
    for text in lines:
        part.line(text)


class ConsoleTest(unittest.TestCase):
    """Console and ConsolePart."""

    def test_lines_wait_while_a_question_is_open(self) -> None:
        """Another part's lines, from another thread, come out after the answer instead of inside the question."""
        out = io.StringIO()
        console = Console(out, terminal=False)
        kubernetes = console.part("Kubernetes")
        disks = console.part("Disks")
        with disks.question():
            out.write(QUESTION)
            writer = threading.Thread(target=kubernetes.line, args=("Read cluster one.",))
            writer.start()
            writer.join()
            self.assertEqual(out.getvalue(), QUESTION)
            out.write("y\n")
        self.assertEqual(out.getvalue(), QUESTION + "y\nRead cluster one.\n")

    def test_a_stop_note_goes_out_at_once_while_a_question_holds_the_lines(self) -> None:
        """A stop's note reaches the terminal's file descriptor at once, on a line of its own, past the held lines."""
        with tempfile.TemporaryDirectory() as folder:
            terminal = os.path.join(folder, "terminal")
            with open(terminal, "w+", encoding="utf-8") as out:
                console = Console(out, terminal=False)
                kubernetes = console.part("Kubernetes")
                disks = console.part("Disks")
                with disks.question():
                    kubernetes.line("Read cluster one.")
                    console.note("Stopped. Nothing in your cloud was changed.")
                    out.seek(0)
                    self.assertEqual(out.read(), "\nStopped. Nothing in your cloud was changed.\n")

    def test_each_line_is_whole_under_eight_threads(self) -> None:
        """Eight threads writing at once through a slow stream never mix their lines."""
        out = SlowStream()
        console = Console(out, terminal=False)
        expected: List[str] = []
        writers = []
        for number in range(8):
            lines = [f"part {number} line {index} " + "x" * 30 for index in range(40)]
            expected.extend(lines)
            writers.append(threading.Thread(target=write_lines, args=(console.part(f"Part {number}"), lines)))
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join()
        self.assertEqual(sorted(out.getvalue().splitlines()), sorted(expected))
        self.assertTrue(out.getvalue().endswith("\n"))

    def test_two_parts_share_one_status_line(self) -> None:
        """On a terminal both parts' progress sits on one line below the lines, in the order the parts were made."""
        out = io.StringIO()
        console = Console(out, terminal=True)
        kubernetes = console.part("Kubernetes")
        disks = console.part("Disks")
        disks.status("12/40 VMs")
        kubernetes.status("3/7 clusters")
        self.assertEqual(screen(out.getvalue()), ["Kubernetes 3/7 clusters · Disks 12/40 VMs"])
        kubernetes.line("Read cluster one.")
        self.assertEqual(screen(out.getvalue()), ["Read cluster one.", "Kubernetes 3/7 clusters · Disks 12/40 VMs"])
        disks.status("done")
        self.assertEqual(screen(out.getvalue()), ["Read cluster one.", "Kubernetes 3/7 clusters · Disks done"])
        console.clear_status()
        console.line("The report is in zoorik-assessment-20260105-0930.pdf.")
        self.assertEqual(
            screen(out.getvalue()),
            ["Read cluster one.", "The report is in zoorik-assessment-20260105-0930.pdf.", ""],
        )

    def test_a_reads_progress_sets_its_parts_status(self) -> None:
        """A part's progress for a read of clusters puts done/total clusters on the status line."""
        out = io.StringIO()
        progress = Console(out, terminal=True).part("Kubernetes").progress("clusters")
        progress(3, 7)
        self.assertEqual(screen(out.getvalue()), ["Kubernetes 3/7 clusters"])

    def test_a_question_takes_the_status_line_and_gives_it_back(self) -> None:
        """The status line makes way for the question, and comes back below the held lines after the answer."""
        out = io.StringIO()
        console = Console(out, terminal=True)
        kubernetes = console.part("Kubernetes")
        disks = console.part("Disks")
        kubernetes.status("3/7 clusters")
        with disks.question():
            out.write(QUESTION)
            kubernetes.line("Read cluster one.")
            kubernetes.status("4/7 clusters")
            # The terminal shows the answer as it is typed.
            out.write("y\n")
        self.assertEqual(screen(out.getvalue()), [QUESTION + "y", "Read cluster one.", "Kubernetes 4/7 clusters"])

    def test_no_status_line_off_a_terminal(self) -> None:
        """In a pipe, a file or CI the status line is never drawn, so the output holds only whole lines."""
        out = io.StringIO()
        console = Console(out, terminal=False)
        kubernetes = console.part("Kubernetes")
        kubernetes.status("3/7 clusters")
        kubernetes.line("Read cluster one.")
        console.clear_status()
        self.assertEqual(out.getvalue(), "Read cluster one.\n")
