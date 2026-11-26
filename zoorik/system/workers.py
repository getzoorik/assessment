"""Parallel reads: a few items at a time, a typed failure as that item's result, and progress for the status line."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Sequence, TypeVar, Union

from zoorik.console import Progress
from zoorik.errors import AssessmentError

Item = TypeVar("Item")
Result = TypeVar("Result")

# Enough reads at once to hide each call's wait, few enough that the clouds don't throttle the run.
WORKERS = 8


def parallel(
    work: Callable[[Item], Result],
    items: Sequence[Item],
    progress: Progress,
) -> List[Union[Result, AssessmentError]]:
    """Run work on each item, WORKERS at a time, and give the results in the items' order.

    A typed failure becomes that item's result, so one cluster or VM that can't be read never stops the others. Any
    other exception is a defect and stops the run. progress(done, total) is called before the first item and after
    each one.
    """
    if not items:
        return []
    total = len(items)
    progress(0, total)
    results: Dict[int, Union[Result, AssessmentError]] = {}
    executor = ThreadPoolExecutor(WORKERS)
    futures = {executor.submit(_guarded, work, item): index for index, item in enumerate(items)}
    try:
        for future in as_completed(futures):
            results[futures[future]] = future.result()
            progress(len(results), total)
    finally:
        # When an item fails with a defect, the items not started yet are dropped instead of run.
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False)
    return [results[index] for index in range(total)]


def _guarded(work: Callable[[Item], Result], item: Item) -> Union[Result, AssessmentError]:
    """Run work on one item, giving a typed failure back as the item's result."""
    try:
        return work(item)
    except AssessmentError as error:
        return error
