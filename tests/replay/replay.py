"""Replay: each call gets its recorded answers in order, and a call with no answer left fails the replay."""

import collections
import os
import threading
from typing import Deque, Dict, Generic, Iterable, List, Mapping, Optional, Sequence, Tuple, Type, TypeVar

from tests.replay.recording import TEMP, Call, Failure, Fetch, call_key
from zoorik.errors import AssessmentError
from zoorik.system.clock import Clock
from zoorik.system.process import Completed

Answer = TypeVar("Answer")

# Each typed failure by its name, so a recorded failure is raised again as its own type.
ERROR_TYPES: Dict[str, Type[AssessmentError]] = {kind.__name__: kind for kind in AssessmentError.__subclasses__()}


class ReplayError(AssertionError):
    """The run asked for something the recording doesn't hold."""


def raised(failure: Failure) -> AssessmentError:
    """Give the typed failure a recording holds, to raise again."""
    return ERROR_TYPES[failure.kind](failure.reason)


class AnswerQueues(Generic[Answer]):
    """Recorded answers by key, each key's in the order they were recorded; nothing answers twice."""

    def __init__(self, answers: Iterable[Tuple[str, Answer]]) -> None:
        """Queue each answer under its key."""
        self._queues: Dict[str, Deque[Answer]] = {}
        for key, answer in answers:
            self._queues.setdefault(key, collections.deque()).append(answer)
        self._lock = threading.Lock()

    def take(self, key: str) -> Answer:
        """Give the next answer for key; a key with no answer left fails the replay."""
        with self._lock:
            queue = self._queues.get(key)
            if not queue:
                raise ReplayError(f"no recorded answer left for {key}")
            return queue.popleft()

    def left(self) -> List[str]:
        """Name the key of each answer nothing took, in key order."""
        with self._lock:
            return sorted(key for key, queue in self._queues.items() for _ in queue)


class ReplayRunner:
    """A CommandRunner that answers from recorded calls, and writes again the files each call wrote."""

    def __init__(self, calls: Sequence[Call], temp_root: str, clock: Clock) -> None:
        """Answer from calls, with the run's temp folder at temp_root, waiting each call's time on clock."""
        self._calls = AnswerQueues((call.key, call) for call in calls)
        self._temp_root = temp_root
        self._clock = clock

    def run(self, argv: Sequence[str], timeout: float, overrides: Mapping[str, str]) -> Completed:
        """Give the next recorded answer for argv, after the time the call took."""
        call = self._calls.take(call_key(argv, self._temp_root))
        self._clock.sleep(call.seconds)
        for path, text in call.files.items():
            self._write(path, text)
        if isinstance(call.outcome, Failure):
            raise raised(call.outcome)
        return Completed(call.outcome.exit, self._local(call.outcome.stdout), self._local(call.outcome.stderr))

    def end_children(self) -> None:
        """End nothing: a replayed call starts no program."""

    def unused(self) -> List[str]:
        """Name the key of each recorded call the run never made."""
        return self._calls.left()

    def _write(self, path: str, text: str) -> None:
        """Write a file a recorded call wrote, in this run's temporary folder."""
        local = self._local(path).replace("/", os.sep)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "w", encoding="utf-8") as file:
            file.write(text)

    def _local(self, text: str) -> str:
        """Write <temp> in text as this run's temporary folder."""
        return text.replace(TEMP, self._temp_root)


class ReplayHttp:
    """An HttpClient that answers from recorded fetches."""

    def __init__(self, fetches: Sequence[Fetch]) -> None:
        """Answer from fetches."""
        self._fetches = AnswerQueues((fetch.url, fetch) for fetch in fetches)

    def get_json(self, url: str) -> object:
        """Give the next recorded answer for url."""
        fetch = self._fetches.take(url)
        if isinstance(fetch.outcome, Failure):
            raise raised(fetch.outcome)
        return fetch.outcome.value

    def unused(self) -> List[str]:
        """Name the URL of each recorded fetch the run never made."""
        return self._fetches.left()


class ReplayPrompt:
    """A Prompt that gives the recorded answers in order."""

    def __init__(self, answers: Sequence[Optional[bool]]) -> None:
        """Answer with answers, in order."""
        self._answers = AnswerQueues(("y/N", answer) for answer in answers)

    def ask(self, question: str) -> Optional[bool]:
        """Give the next recorded answer."""
        return self._answers.take("y/N")

    def unused(self) -> List[str]:
        """Name each recorded answer no question took."""
        return self._answers.left()
