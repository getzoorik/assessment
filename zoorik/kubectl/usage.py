"""Usage: kubectl top samples over a window, kept as each container's peak."""

import re
from dataclasses import dataclass
from typing import Dict, Mapping, NamedTuple, Optional, Sequence

from zoorik.errors import UnexpectedResponse
from zoorik.format import SECONDS_PER_MINUTE

# One sample at the start of the window, then one every 30 seconds to its end.
SAMPLE_INTERVAL = 30
# The window the run samples unless told otherwise.
DEFAULT_SAMPLE_MINUTES = 5
# kubectl top pods -A --containers --no-headers writes namespace, pod, container, CPU and memory.
TOP_COLUMNS = 5
# kubectl top always writes CPU in millicores and memory in mebibytes.
CPU_TEXT = re.compile(r"([0-9]+)m")
MEMORY_TEXT = re.compile(r"([0-9]+)Mi")
MEBIBYTE = 2**20
NO_DATA = "metrics-server returned no data"


@dataclass(frozen=True)
class SampleWindow:
    """How long usage is sampled: a sample at the start, then one every SAMPLE_INTERVAL seconds to the end."""

    minutes: int

    @property
    def seconds(self) -> int:
        """Give the window's length in seconds."""
        return self.minutes * SECONDS_PER_MINUTE

    @property
    def samples(self) -> int:
        """Give how many samples the window takes: two a minute, plus the one at the start."""
        return self.seconds // SAMPLE_INTERVAL + 1


class ContainerKey(NamedTuple):
    """A container, by its namespace, its pod and its own name."""

    namespace: str
    pod: str
    container: str


@dataclass(frozen=True)
class Peak:
    """A container's highest use: CPU in millicores and memory in bytes."""

    cpu: int
    memory: int


@dataclass(frozen=True)
class Usage:
    """Each container's peak over the window, or, when no sample gave a figure, why."""

    peaks: Mapping[ContainerKey, Peak]
    # Why there are no peaks, such as "metrics-server is not installed"; None when the samples gave figures.
    reason: Optional[str]


def top_table(text: str) -> Dict[ContainerKey, Peak]:
    """Read one kubectl top pods -A --containers --no-headers answer; a container listed twice keeps its highest."""
    table: Dict[ContainerKey, Peak] = {}
    for line in text.splitlines():
        columns = line.split()
        if not columns:
            continue
        if len(columns) != TOP_COLUMNS:
            raise _unreadable()
        cpu, memory = CPU_TEXT.fullmatch(columns[3]), MEMORY_TEXT.fullmatch(columns[4])
        if cpu is None or memory is None:
            raise _unreadable()
        key = ContainerKey(columns[0], columns[1], columns[2])
        table[key] = higher(table.get(key), Peak(int(cpu.group(1)), int(memory.group(1)) * MEBIBYTE))
    return table


def _unreadable() -> UnexpectedResponse:
    """Make the failure for a line that is not a container's use; the line names workloads, so it stays out."""
    return UnexpectedResponse("unexpected answer: kubectl top gave a line that is not a container's CPU and memory")


def higher(seen: Optional[Peak], sample: Peak) -> Peak:
    """Keep the highest CPU and the highest memory of a container's peak so far and a new sample."""
    if seen is None:
        return sample
    return Peak(max(seen.cpu, sample.cpu), max(seen.memory, sample.memory))


def peaks(tables: Sequence[Mapping[ContainerKey, Peak]]) -> Dict[ContainerKey, Peak]:
    """Keep each container's highest CPU and memory across the samples that answered."""
    merged: Dict[ContainerKey, Peak] = {}
    for table in tables:
        for key, sample in table.items():
            merged[key] = higher(merged.get(key), sample)
    return merged


def usage_of(tables: Sequence[Mapping[ContainerKey, Peak]], last_failure: Optional[str]) -> Usage:
    """Make the usage from the samples that answered; when none did, keep why the last one failed."""
    merged = peaks(tables)
    if merged:
        return Usage(merged, None)
    if not tables and last_failure is not None:
        return Usage({}, last_failure)
    return Usage({}, NO_DATA)
