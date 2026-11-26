"""Made-up runs for replay tests, with neutral names."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import FrozenSet, Optional, Sequence

from tests.replay.recording import Call, Fetch, RunFacts
from zoorik.system.env import EnvFacts


@dataclass(frozen=True)
class Scenario:
    """A made-up run: its facts, the programs installed, and the answers to its calls, fetches and questions."""

    name: str
    facts: RunFacts
    installed: FrozenSet[str]
    calls: Sequence[Call]
    fetches: Sequence[Fetch]
    answers: Sequence[Optional[bool]]


# A Monday morning, outside Cloud Shell, with a fixed salt so anonymized names are the same on every run.
FACTS = RunFacts(
    started=datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc),
    salt="0123456789abcdef",
    windows=False,
    env=EnvFacts(None, None, None),
)

# Neither cloud CLI is installed.
NO_CLI = Scenario(name="no-cli", facts=FACTS, installed=frozenset(), calls=(), fetches=(), answers=())
