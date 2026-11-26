"""Recordings: the calls, fetches and answers of a real run, redacted, so the run can be replayed offline."""

import dataclasses
import gzip
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from tests.replay.redact import Redactor, page_of
from zoorik.errors import AssessmentError
from zoorik.kubectl.objects import BUDGETS, PODS
from zoorik.system.clock import Clock
from zoorik.system.env import EnvFacts
from zoorik.system.http import HttpClient
from zoorik.system.process import CommandRunner, Completed
from zoorik.system.prompt import Prompt
from zoorik.system.runtime import Runtime

FORMAT_VERSION = 1
# Stands for the run's temporary folder, which has a new name on every run.
TEMP = "<temp>"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The options through which a program writes a file the run reads back, such as a kubeconfig.
FILE_OPTIONS = ("--file", "--kubeconfig")


@dataclass(frozen=True)
class RunFacts:
    """What a run takes from the computer besides programs and prices."""

    started: datetime
    salt: str
    windows: bool
    env: EnvFacts


@dataclass(frozen=True)
class Failure:
    """A typed failure raised instead of an answer: the type's name and the reason."""

    kind: str
    reason: str


@dataclass(frozen=True)
class Call:
    """One program the run started, and what came back."""

    key: str
    argv: List[str]
    outcome: Union[Completed, Failure]
    files: Dict[str, str]
    seconds: float


@dataclass(frozen=True)
class Body:
    """The JSON a fetch gave back, parsed."""

    value: object


@dataclass(frozen=True)
class Fetch:
    """One HTTP GET, and what came back."""

    url: str
    outcome: Union[Body, Failure]


@dataclass(frozen=True)
class Recording:
    """A whole run: its command line and facts, then its calls, fetches and y/N answers in the order they ended."""

    argv: List[str]
    facts: RunFacts
    calls: List[Call]
    fetches: List[Fetch]
    answers: List[Optional[bool]]


class RecordingRefused(Exception):
    """A recording may hold private data, so it is never written inside the repository."""


def call_key(argv: Sequence[str], temp_root: str) -> str:
    """Name a call alike on every run and system: temp paths as <temp> with /, and az @file arguments as their text."""
    return json.dumps([_key_part(arg, temp_root) for arg in argv])


def _key_part(arg: str, temp_root: str) -> str:
    """Give one argument as a key holds it."""
    if arg.startswith("@" + temp_root):
        # On Windows az reads a value from a file; elsewhere the same value is the argument itself.
        with open(arg[1:], encoding="utf-8") as value:
            return value.read()
    return temp_path(arg, temp_root) if temp_root in arg else arg


def scrub(text: str, temp_root: str) -> str:
    """Write the run's temporary folder in text as <temp>."""
    return text.replace(temp_root, TEMP)


def temp_path(path: str, temp_root: str) -> str:
    """Write a path in the temporary folder as <temp>/..., with / between its parts on every system."""
    return scrub(path, temp_root).replace(os.sep, "/")


def file_targets(argv: Sequence[str], temp_root: str) -> List[str]:
    """Name the files in the temporary folder that argv may write: the values of --file and --kubeconfig."""
    inside = temp_root + os.sep
    return [value for option, value in zip(argv, argv[1:]) if option in FILE_OPTIONS and value.startswith(inside)]


def read_text(path: str) -> Optional[str]:
    """Give a file's text, or None when there is no such file."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as text:
        return text.read()


def cluster_of(call: Call) -> str:
    """Name the cluster a kubectl call reads: its kubeconfig."""
    return call.argv[call.argv.index("--kubeconfig") + 1]


class Recorder:
    """Records a real run: its Runtime runs, fetches and asks for real, and each answer is kept, redacted."""

    def __init__(self, argv: Sequence[str], real: Runtime, redactor: Redactor) -> None:
        """Start the recording of the run that argv starts, on the real Runtime."""
        self.recording = Recording(
            list(argv), RunFacts(real.clock.now(), real.salt, real.windows, real.env), [], [], []
        )
        self._redactor = redactor
        self._temp_root = real.temp_root
        self._lock = threading.Lock()
        self.runtime = dataclasses.replace(
            real,
            runner=RecordingRunner(real.runner, self, real.temp_root, real.clock),
            http=RecordingHttp(real.http, self),
            prompt=RecordingPrompt(real.prompt, self),
        )

    def keep_call(
        self,
        key: str,
        argv: Sequence[str],
        outcome: Union[Completed, Failure],
        written: Mapping[str, str],
        seconds: float,
    ) -> None:
        """Keep one call: its output redacted, the files it wrote as stand-ins, and temp paths as <temp>."""
        root = self._temp_root
        if isinstance(outcome, Completed):
            stdout = self._redactor.output(argv, outcome.stdout)
            outcome = Completed(outcome.exit, scrub(stdout, root), scrub(outcome.stderr, root))
        files = {temp_path(path, root): self._redactor.written(text) for path, text in written.items()}
        call = Call(key, [scrub(arg, root) for arg in argv], outcome, files, seconds)
        with self._lock:
            self.recording.calls.append(call)

    def keep_fetch(self, fetch: Fetch) -> None:
        """Keep one fetch."""
        with self._lock:
            self.recording.fetches.append(fetch)

    def keep_answer(self, answer: Optional[bool]) -> None:
        """Keep one y/N answer."""
        with self._lock:
            self.recording.answers.append(answer)

    def finished(self) -> Recording:
        """Give the recording with each cluster's pod pages narrowed together to the labels the readers keep.

        One page can't tell which labels a placement term of a pod on another page, or a disruption budget, uses, so
        this waits for the end of the run. The cluster of a page is its kubeconfig.
        """
        pages: Dict[str, List[Tuple[int, Completed]]] = {}
        budgets: Dict[str, List[str]] = {}
        for index, call in enumerate(self.recording.calls):
            if call.argv[0] != "kubectl" or not isinstance(call.outcome, Completed):
                continue
            if page_of(call.outcome.stdout, PODS):
                pages.setdefault(cluster_of(call), []).append((index, call.outcome))
            elif page_of(call.outcome.stdout, BUDGETS):
                budgets.setdefault(cluster_of(call), []).append(call.outcome.stdout)
        calls = list(self.recording.calls)
        for cluster, found in pages.items():
            pod_pages = [completed.stdout for _, completed in found]
            narrowed = self._redactor.narrow_pod_pages(pod_pages, budgets.get(cluster, []))
            for (index, completed), text in zip(found, narrowed):
                calls[index] = dataclasses.replace(calls[index], outcome=dataclasses.replace(completed, stdout=text))
        return dataclasses.replace(self.recording, calls=calls)


class RecordingRunner:
    """Runs each call for real, and gives it to the recorder with the files it wrote."""

    def __init__(self, real: CommandRunner, recorder: Recorder, temp_root: str, clock: Clock) -> None:
        """Run calls on real and time them with clock."""
        self._real = real
        self._recorder = recorder
        self._temp_root = temp_root
        self._clock = clock

    def run(self, argv: Sequence[str], timeout: float, overrides: Mapping[str, str]) -> Completed:
        """Run argv for real, keep the call, and give back what the program gave."""
        key = call_key(argv, self._temp_root)
        before = {path: read_text(path) for path in file_targets(argv, self._temp_root)}
        started = self._clock.monotonic()
        try:
            completed = self._real.run(argv, timeout, overrides)
        except AssessmentError as error:
            self._keep(key, argv, Failure(type(error).__name__, str(error)), before, started)
            raise
        self._keep(key, argv, completed, before, started)
        return completed

    def end_children(self) -> None:
        """End the real runner's live programs."""
        self._real.end_children()

    def _keep(
        self,
        key: str,
        argv: Sequence[str],
        outcome: Union[Completed, Failure],
        before: Mapping[str, Optional[str]],
        started: float,
    ) -> None:
        """Give the recorder the call, with the files whose text the call changed."""
        after = {path: read_text(path) for path in before}
        written = {path: text for path, text in after.items() if text is not None and text != before[path]}
        self._recorder.keep_call(key, argv, outcome, written, self._clock.monotonic() - started)


class RecordingHttp:
    """Fetches each URL for real, and gives the fetch to the recorder."""

    def __init__(self, real: HttpClient, recorder: Recorder) -> None:
        """Fetch with real."""
        self._real = real
        self._recorder = recorder

    def get_json(self, url: str) -> object:
        """Fetch url for real, keep the fetch, and give back its JSON."""
        try:
            value = self._real.get_json(url)
        except AssessmentError as error:
            self._recorder.keep_fetch(Fetch(url, Failure(type(error).__name__, str(error))))
            raise
        self._recorder.keep_fetch(Fetch(url, Body(value)))
        return value


class RecordingPrompt:
    """Asks for real, and gives each answer to the recorder."""

    def __init__(self, real: Prompt, recorder: Recorder) -> None:
        """Ask with real."""
        self._real = real
        self._recorder = recorder

    def ask(self, question: str) -> Optional[bool]:
        """Ask question for real, keep the answer, and give it back."""
        answer = self._real.ask(question)
        self._recorder.keep_answer(answer)
        return answer


def refuse_inside(path: str, repo: str = REPO) -> None:
    """Refuse path if it is inside repo, however it is spelt: other case, symlinks and .. included."""
    repo_id = os.stat(repo)
    folder = os.path.dirname(os.path.realpath(path))
    while True:
        # Folders are compared as the same folder on disk, which a case-changed or linked spelling still is.
        if os.path.isdir(folder) and os.path.samestat(os.stat(folder), repo_id):
            raise RecordingRefused(f"{path} is inside the repository; write recordings outside it")
        parent = os.path.dirname(folder)
        if parent == folder:
            return
        folder = parent


def save(recording: Recording, path: str, repo: str = REPO) -> None:
    """Write recording to a new file at path, outside repo: gzip JSON that only the user can read on POSIX."""
    refuse_inside(path, repo)
    data = gzip.compress(json.dumps(to_json(recording), sort_keys=True).encode("utf-8"), mtime=0)
    with open(path, "xb", opener=_owner_only) as file:
        file.write(data)


def _owner_only(path: str, flags: int) -> int:
    """Open a new file with mode 600."""
    return os.open(path, flags, 0o600)


def load(path: str) -> Recording:
    """Read a recording that save wrote."""
    with gzip.open(path, "rt", encoding="utf-8") as file:
        data = json.load(file)
    if data["version"] != FORMAT_VERSION:
        raise ValueError(f"{path} is recording format {data['version']}; this harness reads format {FORMAT_VERSION}")
    return from_json(data)


def to_json(recording: Recording) -> Dict[str, object]:
    """Give recording as JSON values."""
    facts = recording.facts
    return {
        "version": FORMAT_VERSION,
        "argv": recording.argv,
        "facts": {
            "started": facts.started.isoformat(),
            "salt": facts.salt,
            "windows": facts.windows,
            "env": dataclasses.asdict(facts.env),
        },
        "calls": [
            {
                "key": call.key,
                "argv": call.argv,
                "outcome": _outcome_json(call.outcome),
                "files": call.files,
                "seconds": call.seconds,
            }
            for call in recording.calls
        ],
        "fetches": [{"url": fetch.url, "outcome": _outcome_json(fetch.outcome)} for fetch in recording.fetches],
        "answers": recording.answers,
    }


def _outcome_json(outcome: Union[Completed, Body, Failure]) -> Dict[str, object]:
    """Give an outcome as JSON values, under the name of its kind."""
    if isinstance(outcome, Failure):
        return {"failure": dataclasses.asdict(outcome)}
    if isinstance(outcome, Completed):
        return {"completed": dataclasses.asdict(outcome)}
    return {"body": outcome.value}


def from_json(data: Mapping[str, Any]) -> Recording:
    """Give the recording that to_json wrote as data."""
    facts = data["facts"]
    return Recording(
        argv=data["argv"],
        facts=RunFacts(
            datetime.fromisoformat(facts["started"]), facts["salt"], facts["windows"], EnvFacts(**facts["env"])
        ),
        calls=[
            Call(
                call["key"],
                call["argv"],
                Failure(**call["outcome"]["failure"])
                if "failure" in call["outcome"]
                else Completed(**call["outcome"]["completed"]),
                call["files"],
                call["seconds"],
            )
            for call in data["calls"]
        ],
        fetches=[
            Fetch(
                fetch["url"],
                Failure(**fetch["outcome"]["failure"])
                if "failure" in fetch["outcome"]
                else Body(fetch["outcome"]["body"]),
            )
            for fetch in data["fetches"]
        ],
        answers=data["answers"],
    )
