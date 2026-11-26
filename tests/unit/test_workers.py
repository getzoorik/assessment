"""Parallel reads."""

import threading
import time
import unittest
from typing import Dict, List, Tuple, Union

from zoorik.errors import AccessDenied, AssessmentError
from zoorik.system.workers import WORKERS, parallel


def no_progress(done: int, total: int) -> None:
    """Drop the progress."""


class Read:
    """Work whose outcome for each item is set in a table."""

    def __init__(self, table: Dict[int, Union[int, Exception]]) -> None:
        """Answer from table."""
        self._table = table

    def __call__(self, item: int) -> int:
        """Return or raise the item's outcome."""
        outcome = self._table[item]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ParallelTest(unittest.TestCase):
    """parallel."""

    def test_results_keep_the_items_order(self) -> None:
        """Results come back in the items' order, whatever order the items finish in."""

        def slower_first(item: int) -> int:
            time.sleep((5 - item) * 0.02)
            return item * 10

        self.assertEqual(parallel(slower_first, [0, 1, 2, 3, 4], no_progress), [0, 10, 20, 30, 40])

    def test_a_typed_failure_is_that_items_result(self) -> None:
        """A typed failure becomes the item's result, and the other items still run."""
        denied = AccessDenied("access denied")
        results: List[Union[int, AssessmentError]] = parallel(Read({1: 1, 2: denied, 3: 3}), [1, 2, 3], no_progress)
        self.assertEqual(results, [1, denied, 3])

    def test_any_other_exception_stops_the_run(self) -> None:
        """An exception that is not a typed failure is a defect, and comes out of parallel."""
        with self.assertRaisesRegex(ValueError, "a defect"):
            parallel(Read({1: 1, 2: ValueError("a defect"), 3: 3}), [1, 2, 3], no_progress)

    def test_progress_counts_up_to_the_total(self) -> None:
        """The progress starts at 0, then counts one more after each item."""
        calls: List[Tuple[int, int]] = []
        parallel(Read({1: 1, 2: 2, 3: 3}), [1, 2, 3], lambda done, total: calls.append((done, total)))
        self.assertEqual(calls, [(0, 3), (1, 3), (2, 3), (3, 3)])

    def test_no_items_no_progress(self) -> None:
        """With no items there is nothing to run and nothing to show."""
        calls: List[Tuple[int, int]] = []
        self.assertEqual(parallel(Read({}), [], lambda done, total: calls.append((done, total))), [])
        self.assertEqual(calls, [])

    def test_at_most_workers_items_run_at_once(self) -> None:
        """No more than WORKERS items run at the same time, and more than one does."""
        lock = threading.Lock()
        running = [0]
        most = [0]

        def track(item: int) -> int:
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            time.sleep(0.02)
            with lock:
                running[0] -= 1
            return item

        parallel(track, list(range(WORKERS * 3)), no_progress)
        self.assertLessEqual(most[0], WORKERS)
        self.assertGreater(most[0], 1)
