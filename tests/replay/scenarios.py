"""Made-up runs for replay tests, with neutral names."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import FrozenSet, Optional, Sequence

from tests.replay.recording import Call, Fetch, RunFacts
from zoorik.system.env import EnvFacts
from zoorik.system.process import Completed


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


def call(argv: Sequence[str], stdout: str = "", *, exit_code: int = 0, stderr: str = "") -> Call:
    """Make a made-up call and its answer: argv as the program gets it, with the temporary folder as <temp>."""
    return Call(
        key=json.dumps(list(argv)),
        argv=list(argv),
        outcome=Completed(exit_code, stdout, stderr),
        files={},
        seconds=0.0,
    )
