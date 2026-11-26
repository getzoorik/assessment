"""Reads that both parts share."""

import threading
import unittest
from concurrent.futures import CancelledError
from typing import List

from zoorik.errors import AccessDenied
from zoorik.system.memo import Memo


class Counted:
    """A read that counts its calls, and may wait for a signal before it answers."""

    def __init__(self, gate: threading.Event) -> None:
        """Answer once gate is set."""
        self.calls: List[str] = []
        self._gate = gate
        self._lock = threading.Lock()

    def __call__(self, key: str) -> str:
        """Count the call, wait for the gate, then answer."""
        with self._lock:
            self.calls.append(key)
        self._gate.wait()
        return key.upper()


def open_gate() -> threading.Event:
    """Give a gate that is already open."""
    gate = threading.Event()
    gate.set()
    return gate


class MemoTest(unittest.TestCase):
    """Memo."""

    def test_each_key_is_read_once(self) -> None:
        """A second ask for a key gives the kept value, and another key is read apart."""
        read = Counted(open_gate())
        memo: Memo[str, str] = Memo(read)
        self.assertEqual([memo.get("one"), memo.get("one"), memo.get("two")], ["ONE", "ONE", "TWO"])
        self.assertEqual(read.calls, ["one", "two"])

    def test_threads_that_ask_at_once_share_one_read(self) -> None:
        """Threads that ask while the first read runs wait for it, and none reads again."""
        gate = threading.Event()
        read = Counted(gate)
        memo: Memo[str, str] = Memo(read)
        results: List[str] = []
        lock = threading.Lock()

        def ask() -> None:
            value = memo.get("one")
            with lock:
                results.append(value)

        threads = [threading.Thread(target=ask) for _ in range(8)]
        for thread in threads:
            thread.start()
        gate.set()
        for thread in threads:
            thread.join()
        self.assertEqual(results, ["ONE"] * 8)
        self.assertEqual(read.calls, ["one"])

    def test_a_typed_failure_is_kept_and_raised_on_every_get(self) -> None:
        """Filling a key keeps a typed failure without raising it; each get raises it, and it is not read again."""
        calls: List[str] = []

        def denied(key: str) -> str:
            calls.append(key)
            raise AccessDenied("access denied")

        memo: Memo[str, str] = Memo(denied)
        memo.fill("one")
        for _ in range(2):
            with self.assertRaisesRegex(AccessDenied, "access denied"):
                memo.get("one")
        self.assertEqual(calls, ["one"])

    def test_a_defect_comes_out_and_leaves_no_value(self) -> None:
        """An exception that is not a typed failure comes out of the read, and later asks get no value."""

        def broken(key: str) -> str:
            raise ValueError("a defect")

        memo: Memo[str, str] = Memo(broken)
        with self.assertRaisesRegex(ValueError, "a defect"):
            memo.get("one")
        with self.assertRaises(CancelledError):
            memo.get("one")

    def test_a_thread_waiting_on_a_defect_is_woken(self) -> None:
        """A thread that waits on a read that ends in a defect stops waiting, with no value."""
        gate = threading.Event()
        started = threading.Event()

        def broken(key: str) -> str:
            started.set()
            gate.wait()
            raise ValueError("a defect")

        memo: Memo[str, str] = Memo(broken)
        outcomes: List[str] = []

        def ask() -> None:
            try:
                memo.get("one")
            except ValueError:
                outcomes.append("defect")
            except CancelledError:
                outcomes.append("no value")

        first = threading.Thread(target=ask)
        first.start()
        started.wait()
        second = threading.Thread(target=ask)
        second.start()
        gate.set()
        for thread in (first, second):
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(outcomes), ["defect", "no value"])
