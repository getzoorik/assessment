"""Reads that both parts share: each key is read once, however many threads ask, and a typed failure is kept."""

import threading
from concurrent.futures import Future, wait
from typing import Callable, Dict, Generic, Hashable, TypeVar

from zoorik.errors import AssessmentError

Key = TypeVar("Key", bound=Hashable)
Value = TypeVar("Value")


class Memo(Generic[Key, Value]):
    """Keeps what read gave for each key, or the typed failure it raised, so no cloud read is made twice."""

    def __init__(self, read: Callable[[Key], Value]) -> None:
        """Read each key with read, the first time it is asked for."""
        self._read = read
        self._lock = threading.Lock()
        self._futures: Dict[Key, Future[Value]] = {}

    def get(self, key: Key) -> Value:
        """Give the value for key, reading it on the first ask; a kept failure is raised again."""
        return self._settled(key).result()

    def fill(self, key: Key) -> None:
        """Read key ahead of the first get; a typed failure is kept for get to raise, and not raised here."""
        self._settled(key)

    def _settled(self, key: Key) -> "Future[Value]":
        """Give key's future once it holds a value or a failure; the first thread to ask reads, the others wait."""
        with self._lock:
            future = self._futures.get(key)
            first = future is None
            if future is None:
                future = Future()
                self._futures[key] = future
        if first:
            try:
                future.set_result(self._read(key))
            except AssessmentError as error:
                future.set_exception(error)
            finally:
                if not future.done():
                    # A defect leaves no value: the threads waiting on it stop too, while this one raises it. A
                    # cancel alone doesn't wake wait(); set_running_or_notify_cancel does.
                    future.cancel()
                    future.set_running_or_notify_cancel()
        wait([future])
        return future
