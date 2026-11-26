"""The whole command on made-up estates: exit codes, lines and reports against goldens, stops, errors and options."""

import contextlib
import io
import os
import re
import shutil
import signal
import tempfile
import threading
import unittest
from typing import Callable, Dict, Iterator, List, Mapping, NoReturn, Optional, Sequence, Set

from tests.replay import golden
from tests.replay.estates import (
    FIRST_NAME,
    GOLDEN,
    Estate,
    disks_only,
    invoked_cluster,
    nothing_found,
    signed_out,
    whole_estate,
)
from tests.replay.fake_cloud import CapturingConsole, FakeCloud, FakeRun, Stopped
from tests.replay.replay import ReplayHttp, ReplayPrompt
from tests.replay.scenarios import ACCOUNT
from zoorik import CONTACT, __version__
from zoorik.cli import ASSESS, KUBERNETES, NO_CLI, START, Options, options, run
from zoorik.console import RUN
from zoorik.system.process import Completed
from zoorik.system.prompt import Prompt
from zoorik.system.runtime import Runtime

# The report folder as a golden writes it, with / after it on every system.
OUT = "<out>/"
# The version as a golden writes it, so that a new version changes no golden.
VERSION = "<version>"
# How long a test waits for a thread of the run; a run on made-up answers takes well under a second.
TIMEOUT = 30.0
BY_NAME = {estate.name: estate for estate in GOLDEN}


class Harness:
    """One run of the command on an estate's answers, in folders of its own, with fakes a test may swap first."""

    def __init__(self, estate: Estate) -> None:
        """Make the run's temporary folder and report folder, the report folder holding the estate's files."""
        self.estate = estate
        self.scratch = tempfile.mkdtemp(prefix="zoorik-test-")
        self.temp_root = os.path.join(self.scratch, "temp")
        self.out = os.path.join(self.scratch, "out")
        os.mkdir(self.temp_root)
        os.mkdir(self.out)
        for name in estate.existing:
            with open(os.path.join(self.out, name), "wb") as existing:
                existing.write(b"an earlier report")
        self.fake = FakeRun(estate.scenario.facts, self.temp_root)
        self.cloud = FakeCloud(estate.scenario, self.temp_root, self.fake.clock)
        self.http = ReplayHttp(estate.scenario.fetches)
        self.answers = ReplayPrompt(estate.scenario.answers)
        self.prompt: Prompt = self.answers

    def runtime(self) -> Runtime:
        """Build the run's Runtime from the fakes as they are now."""
        return self.fake.runtime(self.cloud, self.http, self.prompt)

    def run(self) -> int:
        """Run zoorik assess with the estate's options, its report going to the report folder."""
        return run(["assess", "--out", self.out, *self.estate.options], self.runtime())

    def unused(self) -> List[str]:
        """Name each call, fetch and answer of the estate the run never took."""
        return [*self.cloud.unused(), *self.http.unused(), *self.answers.unused()]

    def reports(self) -> Dict[str, bytes]:
        """Give each file the run wrote in the report folder, by name."""
        written: Dict[str, bytes] = {}
        for name in sorted(os.listdir(self.out)):
            if name not in self.estate.existing:
                with open(os.path.join(self.out, name), "rb") as report:
                    written[name] = report.read()
        return written

    def pages(self) -> List[List[str]]:
        """Give the lines of text of each page of each report the run wrote."""
        return [page for data in self.reports().values() for page in golden.text(data)]

    def document(self, code: int) -> Dict[str, object]:
        """Give what the estate's golden holds: the exit code, each part's lines, the files and each report."""
        lines = self.fake.console.lines
        return {
            "exit": code,
            "console": {part: [self.shown(line) for line in lines[part]] for part in sorted(lines)},
            "files": sorted(os.listdir(self.out)),
            "reports": {name: golden.pdf_facts(data) for name, data in self.reports().items()},
        }

    def shown(self, line: str) -> str:
        """Give a console line as a golden writes it: the report folder and the start line's version as placeholders."""
        start = START.format(version=__version__)
        return line.replace(self.out + os.sep, OUT).replace(start, START.format(version=VERSION))

    def close(self) -> None:
        """Remove both folders."""
        shutil.rmtree(self.scratch)


def finished(estate: Estate) -> "Finished":
    """Run the command on estate to its end, and keep what a test reads afterwards."""
    harness = Harness(estate)
    try:
        code = harness.run()
        return Finished(code, harness)
    finally:
        harness.close()


class Finished:
    """A run of the command that ended: its exit code, its golden document and what it left behind."""

    def __init__(self, code: int, harness: Harness) -> None:
        """Keep what the run gave, before its folders go."""
        self.code = code
        self.document = harness.document(code)
        self.lines: Mapping[str, List[str]] = harness.fake.console.lines
        self.notes = harness.fake.console.notes
        self.pages = harness.pages()
        self.unused = harness.unused()
        self.temp_left = os.path.exists(harness.temp_root)


def report_text(estate: Estate) -> str:
    """Give the text of the report a run of estate writes; a table breaks a name after its hyphens, so join them."""
    return re.sub(r"-\n", "-", "\n".join(line for page in finished(estate).pages for line in page))


def word(text: str) -> "re.Pattern[str]":
    """Give a pattern that finds text as a whole name, not inside a longer one."""
    return re.compile(rf"(?<![\w-]){re.escape(text)}(?![\w-])")


class Ended(BaseException):
    """Ends a thread of a stopped run once its test is done with it, past every handler of the run."""


class Gate:
    """A point a thread of the run waits at until the test opens it."""

    def __init__(self) -> None:
        """Start closed and not reached."""
        self.reached = threading.Event()
        self.opened = threading.Event()

    def hold(self) -> None:
        """Say the gate is reached, and wait here until the test opens it."""
        self.reached.set()
        if not self.opened.wait(TIMEOUT):
            raise AssertionError("the test never opened the gate")

    def end(self) -> NoReturn:
        """Wait here until the test opens the gate, then end the thread."""
        self.hold()
        raise Ended()

    def wait_until_reached(self) -> None:
        """Wait until a thread of the run is at the gate."""
        if not self.reached.wait(TIMEOUT):
            raise AssertionError("the run never reached the gate")


class GatedCloud(FakeCloud):
    """The estate's fake cloud, with the calls that start with held passing the gate first, by then."""

    def __init__(self, harness: Harness, held: Sequence[str], then: Callable[[], None]) -> None:
        """Answer as harness's estate says, but pass the calls that start with held through then first."""
        super().__init__(harness.estate.scenario, harness.temp_root, harness.fake.clock)
        self._held = list(held)
        self._then = then

    def run(self, argv: Sequence[str], timeout: float, overrides: Mapping[str, str]) -> Completed:
        """Hold a call that starts with held at the gate, then answer it as the estate says."""
        if list(argv[: len(self._held)]) == self._held:
            self._then()
        return super().run(argv, timeout, overrides)


class BrokenCloud(FakeCloud):
    """A fake cloud whose every call fails with an error no reader expects, as a defect would."""

    def run(self, argv: Sequence[str], timeout: float, overrides: Mapping[str, str]) -> Completed:
        """Fail at once."""
        raise ValueError("a defect")


class HeldPrompt:
    """A prompt that holds its question open at the gate, where the run's thread ends."""

    def __init__(self, gate: Gate) -> None:
        """Hold each question at gate."""
        self._gate = gate

    def ask(self, question: str) -> Optional[bool]:
        """Hold the question open until the gate opens; the thread ends there."""
        self._gate.end()


class WatchedConsole(CapturingConsole):
    """A captured console that tells a test once a part has tried to write a line, whether it came out or was held."""

    def __init__(self) -> None:
        """Know of no part's line yet."""
        super().__init__()
        self._tried: Set[str] = set()
        self._changed = threading.Condition()

    def _line(self, part: str, text: str) -> None:
        """Write or hold the line, then tell whoever waits for the part."""
        super()._line(part, text)
        with self._changed:
            self._tried.add(part)
            self._changed.notify_all()

    def wait_for_line(self, part: str) -> None:
        """Wait until part has tried to write a line."""
        with self._changed:
            if not self._changed.wait_for(lambda: part in self._tried, TIMEOUT):
                raise AssertionError(f"{part} wrote no line")


class OpeningPrompt:
    """Answers as the estate says; at the first question, opens the gate and waits for a Kubernetes line."""

    def __init__(self, answers: Prompt, gate: Gate, console: WatchedConsole) -> None:
        """Answer from answers; open gate at the first question and watch console."""
        self._answers = answers
        self._gate = gate
        self._console = console
        # The Kubernetes lines out while the first question was open; None until it was asked.
        self.out_during_question: Optional[List[str]] = None

    def ask(self, question: str) -> Optional[bool]:
        """At the first question, let the Kubernetes part go on and wait for its line; then answer."""
        if self.out_during_question is None:
            self._gate.opened.set()
            self._console.wait_for_line(KUBERNETES)
            self.out_during_question = list(self._console.lines.get(KUBERNETES, []))
        return self._answers.ask(question)


class Background:
    """The command in a thread of its own, so the test's thread can stop it as a signal's handler would."""

    def __init__(self, harness: Harness) -> None:
        """Start the run of harness."""
        self.ended_with: Optional[BaseException] = None
        self._harness = harness
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Run the command, and keep what ended it."""
        try:
            self._harness.run()
        except BaseException as error:
            self.ended_with = error

    def join(self) -> None:
        """Wait for the run's thread to end."""
        self._thread.join(TIMEOUT)
        if self._thread.is_alive():
            raise AssertionError("the run did not end")


def stop_at_gate(test: unittest.TestCase, harness: Harness, gate: Gate) -> int:
    """Run harness in the background, fire Ctrl-C once it is at gate, and give the code the stop exits with."""
    background = Background(harness)
    try:
        gate.wait_until_reached()
        with test.assertRaises(Stopped) as stopped:
            harness.fake.stop.fire(signal.SIGINT)
    finally:
        gate.opened.set()
        background.join()
    test.assertIsInstance(background.ended_with, Ended)
    return stopped.exception.code


class CommandGoldenTest(unittest.TestCase):
    """Each estate's run matches its golden, takes every answer it was given and removes its temporary folder."""

    def test_each_estate_matches_its_golden(self) -> None:
        """The exit code, each part's lines, the files written and each report are the estate's golden."""
        for estate in GOLDEN:
            with self.subTest(estate.name):
                run_of = finished(estate)
                self.assertEqual(run_of.unused, [])
                self.assertEqual(run_of.notes, [])
                self.assertFalse(run_of.temp_left)
                golden.check(f"cli_{estate.name.replace('-', '_')}", run_of.document)


class ExitCodeTest(unittest.TestCase):
    """The exit codes and the lines of the runs that write no report."""

    def test_each_estate_exits_with_its_code(self) -> None:
        """A report exits 0; nothing found exits 1 only when something couldn't be read; no CLI at all exits 0."""
        codes = {name: finished(estate).code for name, estate in BY_NAME.items()}
        failed = {name for name, code in codes.items() if code}
        self.assertEqual(failed, {"nothing-readable", "signed-out"})
        self.assertEqual(set(codes.values()), {0, 1})

    def test_without_either_cli_the_run_says_one_line(self) -> None:
        """Neither az nor aws installed: one line, and exit 0."""
        run_of = finished(BY_NAME["no-cli"])
        self.assertEqual((run_of.code, dict(run_of.lines)), (0, {RUN: [NO_CLI]}))

    def test_signed_out_the_run_names_each_cloud(self) -> None:
        """Both CLIs installed and signed out: one line for each cloud, and exit 1."""
        run_of = finished(BY_NAME["signed-out"])
        lines = [
            "Azure: not signed in to Azure; run az login",
            "AWS: no AWS credentials; run aws configure or aws sso login",
        ]
        self.assertEqual((run_of.code, dict(run_of.lines)), (1, {RUN: lines}))

    def test_nothing_found_writes_no_report(self) -> None:
        """An estate with nothing priced writes no file; the run's last line says so."""
        for name in ("nothing-found", "nothing-readable"):
            with self.subTest(name):
                run_of = finished(BY_NAME[name])
                self.assertEqual(run_of.document["files"], [])
                self.assertTrue(run_of.lines[RUN][-1].startswith("Nothing to report:"))

    def test_an_existing_report_is_never_replaced(self) -> None:
        """A report of the same minute stays as it was, and the new one takes -2."""
        run_of = finished(BY_NAME["existing-report"])
        self.assertEqual(run_of.document["files"], sorted([FIRST_NAME, "zoorik-assessment-20260105-0930-2.pdf"]))

    def test_a_report_folder_that_is_a_file_stops_the_run_before_any_read(self) -> None:
        """--out naming a file stops the run with exit 1, and no cloud is read."""
        harness = Harness(nothing_found())
        try:
            path = os.path.join(harness.out, "taken")
            with open(path, "w", encoding="utf-8"):
                pass
            code = run(["assess", "--out", path], harness.runtime())
            self.assertEqual(code, 1)
            self.assertEqual(
                harness.fake.console.lines, {RUN: [f"Cannot write the report: {path} is a file; --out takes a folder"]}
            )
            self.assertEqual(len(harness.cloud.unused()), len(harness.estate.scenario.calls))
            self.assertFalse(os.path.exists(harness.temp_root))
        finally:
            harness.close()


class WholeEstateTest(unittest.TestCase):
    """Page 1 of the whole estate, against the figures its estate works out by hand."""

    def test_page_one_adds_both_parts(self) -> None:
        """$1,109 of machines and $250 of disks are $1,359 today and $339 after: a saving of $1,020, 75%, in five rows.

        Page 1's figures, in the order it draws them: the headline's saving and share, the four figures with the
        saving a year, the rows and their total, then what the cost today holds. Spot saves nothing on these clusters.
        """
        page = finished(whole_estate()).pages[0]
        figure = re.compile(r"\(?\u2212?\$[\d,]+\)?,?|\(?\d+%\)?")
        figures = [line for line in page if figure.fullmatch(line)]
        expected = ["$1,020", "(75%)", "$1,359", "$339", "\u2212$1,020", "$12,240", "75%"]
        rows = ["\u2212$540", "\u2212$139", "\u2212$124", "$0", "\u2212$217"]
        self.assertEqual(figures, [*expected, *rows, "\u2212$1,020", "($1,109)", "($250),"])


class AnonymizeTest(unittest.TestCase):
    """--anonymize hides every name the estate gave, where the report without it shows them."""

    def test_no_name_of_the_estate_is_in_the_report(self) -> None:
        """Each name the plain report shows is hashed under --anonymize; machine types and regions stay."""
        plain, hidden = report_text(whole_estate()), report_text(BY_NAME["anonymize"])
        names = [
            "cluster-public",
            "cluster-eks",
            "system",
            "apps-on-demand",
            "apps-spot",
            "workers-self",
            "burst",
            "shop",
            "web",
            "worker",
            "vm-one",
            "i-07",
            "subscription-one",
            ACCOUNT,
        ]
        for name in names:
            with self.subTest(name):
                self.assertRegex(plain, word(name))
                self.assertNotRegex(hidden, word(name))
        # The workloads a and b are also words of the report's own text, so their hashes are counted instead: the four
        # workloads, and a and b again as the apps that run fewer copies at quiet times.
        self.assertEqual(len(re.findall(r"(?<![\w-])wl-[0-9a-f]{8}(?![\w-])", hidden)), 6)
        for kept in ("westeurope", "eu-west-1", "m6i.large", "m6i.2xlarge"):
            with self.subTest(kept):
                self.assertRegex(hidden, word(kept))


class StopTest(unittest.TestCase):
    """Ctrl-C ends the run at once with its note, its temporary folder removed and its programs ended."""

    def test_a_stop_during_command_invoke_names_it(self) -> None:
        """A stop while command invoke runs in a cluster exits 130 and says it had started there."""
        harness = Harness(invoked_cluster())
        gate = Gate()
        harness.cloud = GatedCloud(harness, ["az", "aks", "command", "invoke"], gate.end)
        try:
            code = stop_at_gate(self, harness, gate)
            self.assertEqual(code, 130)
            note = (
                "Stopped. az aks command invoke had already started in 1 cluster (see the activity log); nothing "
                "else in your cloud was changed."
            )
            self.assertEqual(harness.fake.console.notes, [note])
            self.assertFalse(os.path.exists(harness.temp_root))
            self.assertEqual(harness.cloud.ended, 1)
        finally:
            harness.close()

    def test_a_stop_while_the_question_is_open_writes_its_note(self) -> None:
        """A stop while the y/N question waits for its answer exits 130 with its note all the same."""
        harness = Harness(disks_only())
        gate = Gate()
        harness.prompt = HeldPrompt(gate)
        try:
            code = stop_at_gate(self, harness, gate)
            self.assertEqual(code, 130)
            self.assertEqual(harness.fake.console.notes, ["Stopped. Nothing in your cloud was changed."])
            self.assertFalse(os.path.exists(harness.temp_root))
            self.assertEqual(harness.cloud.ended, 1)
        finally:
            harness.close()

    def test_an_unexpected_error_stops_the_run_with_one_line(self) -> None:
        """An error no reader expects ends the run with exit 1 and one line naming where it was, to send to Zoorik."""
        harness = Harness(signed_out())
        harness.cloud = BrokenCloud(harness.estate.scenario, harness.temp_root, harness.fake.clock)
        try:
            with self.assertRaises(Stopped) as stopped:
                harness.run()
            self.assertEqual(stopped.exception.code, 1)
            [note] = harness.fake.console.notes
            where = r"ValueError at zoorik/cloud/cli\.py:\d+"
            sent = re.escape(f"Nothing in your cloud was changed. Please send this line to {CONTACT}.")
            self.assertRegex(note, rf"^Stopped by an unexpected error \({where}\)\. {sent}$")
            self.assertFalse(os.path.exists(harness.temp_root))
            self.assertEqual(harness.cloud.ended, 1)
        finally:
            harness.close()


class HeldLinesTest(unittest.TestCase):
    """Lines wait while the y/N question is open, and follow its answer."""

    def test_a_kubernetes_line_waits_for_the_answer(self) -> None:
        """A Kubernetes line written while the question is open comes out after it, and the run is the golden's."""
        harness = Harness(whole_estate())
        gate = Gate()
        console = WatchedConsole()
        harness.fake.console = console
        # The Kubernetes part starts only once the question is open.
        harness.cloud = GatedCloud(harness, ["az", "aks", "list"], gate.hold)
        prompt = OpeningPrompt(harness.answers, gate, console)
        harness.prompt = prompt
        try:
            code = harness.run()
            self.assertEqual(prompt.out_during_question, [])
            self.assertEqual(harness.unused(), [])
            golden.check("cli_whole_estate", harness.document(code))
        finally:
            harness.close()


@contextlib.contextmanager
def quiet() -> Iterator[None]:
    """Keep argparse's text off the test's output."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


class OptionsTest(unittest.TestCase):
    """The command takes assess with four options, plus --help and --version, and nothing else."""

    def usage(self, argv: List[str]) -> int:
        """Give the code argv exits with, once the run's temporary folder is removed; argparse's text is kept."""
        harness = Harness(nothing_found())
        try:
            with quiet(), self.assertRaises(SystemExit) as exited:
                run(argv, harness.runtime())
            self.assertFalse(os.path.exists(harness.temp_root))
            self.assertEqual(len(harness.cloud.unused()), len(harness.estate.scenario.calls))
        finally:
            harness.close()
        code = exited.exception.code
        # argparse exits with a number.
        assert isinstance(code, int)
        return code

    def listed(self, argv: List[str]) -> Set[str]:
        """Give the options argv's help lists."""
        text = io.StringIO()
        with contextlib.redirect_stdout(text), self.assertRaises(SystemExit):
            options(argv)
        return set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", text.getvalue()))

    def test_the_help_lists_only_the_four_options(self) -> None:
        """The command lists --help and --version; assess lists --help and its four options."""
        self.assertEqual(self.listed(["--help"]), {"--help", "--version"})
        self.assertEqual(
            self.listed(["assess", "--help"]), {"--help", "--out", "--sample-minutes", "--no-invoke", "--anonymize"}
        )

    def test_the_help_gives_the_command_one_line(self) -> None:
        """Zoorik --help names assess once, with what it does."""
        text = io.StringIO()
        with contextlib.redirect_stdout(text), self.assertRaises(SystemExit):
            options(["--help"])
        named = [line for line in text.getvalue().splitlines() if line.split()[:1] == ["assess"]]
        self.assertEqual(len(named), 1)
        self.assertIn(ASSESS.split()[0], named[0])

    def test_help_and_version_exit_0(self) -> None:
        """--help, assess --help and --version exit 0."""
        for argv in (["--help"], ["assess", "--help"], ["--version"]):
            with self.subTest(argv):
                self.assertEqual(self.usage(argv), 0)

    def test_every_removed_option_and_other_words_exit_2(self) -> None:
        """The removed options, --non-production, no command, another command and a negative window are usage errors."""
        refused = [
            ["assess", "--context", "one"],
            ["assess", "--all-contexts"],
            ["assess", "--discover", "azure"],
            ["assess", "--subscription", "one"],
            ["assess", "--region", "eu-west-1"],
            ["assess", "--no-prices"],
            ["assess", "--anonymize-clusters"],
            ["assess", "--non-production"],
            ["assess", "--anon"],
            ["assess", "kubernetes"],
            ["assess", "--sample-minutes", "-1"],
            ["disks"],
            [],
        ]
        for argv in refused:
            with self.subTest(argv):
                self.assertEqual(self.usage(argv), 2)

    def test_the_defaults(self) -> None:
        """No option: the current folder, 5 minutes of samples, command invoke where it is needed, names shown."""
        self.assertEqual(options(["assess"]), Options(None, 5, False, False))
        chosen = options(["assess", "--out", "folder", "--sample-minutes", "0", "--no-invoke", "--anonymize"])
        self.assertEqual(chosen, Options("folder", 0, True, True))
