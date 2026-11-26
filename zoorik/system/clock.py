"""The run's clock, behind a protocol so replays run on virtual time."""

import time
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    """The time of day for the report, a steady clock for durations, and waiting."""

    def now(self) -> datetime:
        """Give the time now, in UTC."""

    def monotonic(self) -> float:
        """Give seconds from a fixed point, which only moves forward."""

    def sleep(self, seconds: float) -> None:
        """Wait for seconds."""


class SystemClock:
    """The computer's clock."""

    def now(self) -> datetime:
        """Give the time now, in UTC."""
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        """Give seconds from a fixed point, which only moves forward."""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """Wait for seconds."""
        time.sleep(seconds)
