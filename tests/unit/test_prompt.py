"""The y/N question."""

import io
import os
import sys
import unittest
from unittest import mock

from zoorik.system.prompt import TerminalPrompt

if sys.platform == "win32":
    import msvcrt
else:
    import pty
    import select

QUESTION = "Read disk use inside the VMs? [y/N] "


class TerminalInput(io.StringIO):
    """Input that says it is a terminal."""

    def isatty(self) -> bool:
        """Say it is a terminal."""
        return True


class TypedAfterQuestion(io.StringIO):
    """The terminal's screen: once the question is shown, the person types answer at controller."""

    def __init__(self, controller: int, answer: bytes) -> None:
        """Type answer at the terminal's controller side once the question is flushed."""
        super().__init__()
        self._controller = controller
        self._answer = answer

    def flush(self) -> None:
        """Show what was written, and type the answer once."""
        super().flush()
        if self.getvalue() and self._answer:
            os.write(self._controller, self._answer)
            self._answer = b""


class TerminalPromptTest(unittest.TestCase):
    """TerminalPrompt."""

    def test_no_terminal_gives_none(self) -> None:
        """Without a terminal nobody can answer, so nothing is asked and the answer is None."""
        stdout = io.StringIO()
        self.assertIsNone(TerminalPrompt(io.StringIO("y\n"), stdout).ask(QUESTION))
        self.assertEqual(stdout.getvalue(), "")

    def test_y_or_yes_is_yes_and_anything_else_is_no(self) -> None:
        """The answers y and yes, in any case, are yes; anything else, an empty line included, is no."""
        answers = {"y\n": True, " YES \n": True, "n\n": False, "\n": False, "yeah\n": False}
        for typed, expected in answers.items():
            with self.subTest(typed=typed), mock.patch("zoorik.system.prompt.flush_pending_keys"):
                stdout = io.StringIO()
                self.assertEqual(TerminalPrompt(TerminalInput(typed), stdout).ask(QUESTION), expected)
                self.assertEqual(stdout.getvalue(), QUESTION)

    def test_input_that_ends_without_enter_is_no(self) -> None:
        """Input ending without Enter is no, and the next line starts on a line of its own."""
        stdout = io.StringIO()
        with mock.patch("zoorik.system.prompt.flush_pending_keys"):
            self.assertFalse(TerminalPrompt(TerminalInput(""), stdout).ask(QUESTION))
        self.assertEqual(stdout.getvalue(), QUESTION + "\n")

    @unittest.skipIf(sys.platform == "win32", "a POSIX terminal")
    def test_keys_typed_before_the_question_do_not_answer_it(self) -> None:
        """A y typed before the question is dropped; the n typed after it is the answer."""
        if sys.platform != "win32":
            controller, terminal = pty.openpty()
            self.addCleanup(os.close, controller)
            stdin = os.fdopen(terminal, "r", encoding="utf-8")
            self.addCleanup(stdin.close)
            os.write(controller, b"y\n")
            ready, _, _ = select.select([terminal], [], [], 10)
            self.assertEqual(ready, [terminal])
            stdout = TypedAfterQuestion(controller, b"n\n")
            self.assertIs(TerminalPrompt(stdin, stdout).ask(QUESTION), False)
            self.assertEqual(stdout.getvalue(), QUESTION)

    @unittest.skipUnless(sys.platform == "win32", "the Windows console")
    def test_keys_typed_before_the_question_are_dropped_on_windows(self) -> None:
        """Every key the console holds is read away before the question is asked."""
        if sys.platform == "win32":
            with mock.patch.object(msvcrt, "kbhit", side_effect=[True, True, False]), mock.patch.object(
                msvcrt, "getwch", return_value="y"
            ) as getwch:
                self.assertIs(TerminalPrompt(TerminalInput("n\n"), io.StringIO()).ask(QUESTION), False)
            self.assertEqual(getwch.call_count, 2)
