"""The command-invoke read: one az aks command invoke per cluster, every kind and the window's peaks in one answer.

The script runs inside the cluster. It lists each kind through the jq filter generated from the allowlist, takes the
window's kubectl top samples and reduces them there to one table of each container's peak, so the answer holds one
table whatever the window. Everything goes back through gzip and base64.
"""

import base64
import dataclasses
import gzip
import zlib
from typing import Dict, List, Optional

from zoorik.cloud.azure import AksCluster, AzureCli
from zoorik.cloud.fields import parse_json
from zoorik.errors import ClusterUnreadable, UnexpectedResponse
from zoorik.kubectl.api import PAGE_SIZE, REQUEST_TIMEOUT, Snapshot, snapshot
from zoorik.kubectl.direct import failure, listing_failure
from zoorik.kubectl.objects import KINDS, Json, Kind, jq_filter
from zoorik.kubectl.usage import SAMPLE_INTERVAL, SampleWindow, Usage, top_table, usage_of

# The time the reads take besides the window: every kind's list, and the answer's way back through Azure.
INVOKE_READ_SECONDS = 600
# The command-invoke pod runs in this namespace; it is Azure's pod, not the customer's.
INVOKE_NAMESPACE = "aks-command"
TOO_LARGE = "its inventory is larger than command invoke can return (512 KB)"
MARK = "@@"
BEGIN = f"{MARK}begin"
END = f"{MARK}end"
USAGE = "usage"
# Runs a command as one section: its name, then what it printed, or ! and the start of its error when it failed.
SECTION = (
    r's(){ n=$1; shift; echo "@@$n"; if "$@" >"$d/o" 2>"$d/e"; then cat "$d/o"; '
    r"""else printf '!%s\n' "$(head -c 300 "$d/e" | tr '\n' ' ')"; fi; }"""
)
# Lists one kind and keeps of it what its jq filter keeps.
LIST = (
    f'g(){{ kubectl get "$1" -A -o json --chunk-size={PAGE_SIZE} --request-timeout={REQUEST_TIMEOUT} >"$d/j" '
    f'&& jq -c "$2" "$d/j"; }}'
)
# Keeps each container's highest CPU and memory over the samples. kubectl top always writes CPU in m and memory in Mi;
# any other line passes through, for the reader to refuse.
PEAK_AWK = (
    'NF == 5 && $4 ~ /^[0-9]+m$/ && $5 ~ /^[0-9]+Mi$/ { k = $1 " " $2 " " $3; '
    "c = substr($4, 1, length($4) - 1) + 0; m = substr($5, 1, length($5) - 2) + 0; "
    "if (!(k in cpu) || c > cpu[k]) cpu[k] = c; if (!(k in mem) || m > mem[k]) mem[k] = m; next } "
    "NF > 0 { print } "
    'END { for (k in cpu) printf "%s %dm %dMi\\n", k, cpu[k], mem[k] }'
)


def sampler(window: SampleWindow) -> str:
    """Write the shell function that takes window's samples and prints their peaks, or fails when none answered."""
    top = f"kubectl top pods -A --containers --no-headers --request-timeout={REQUEST_TIMEOUT}"
    return (
        f't(){{ a=; i=1; while :; do if {top} >"$d/t" 2>"$d/f"; then cat "$d/t" >>"$d/u"; a=1; fi; '
        f'[ "$i" -ge {window.samples} ] && break; i=$((i+1)); sleep {SAMPLE_INTERVAL}; done; '
        f'[ -n "$a" ] || {{ cat "$d/f" >&2; return 1; }}; awk \'{PEAK_AWK}\' "$d/u"; }}'
    )


def script(window: SampleWindow) -> str:
    """Write the one shell line that reads every kind and the window's peaks and prints them in gzip and base64."""
    sections = [f"s {kind.name} g {kind.resource} '{jq_filter(kind)}'" for kind in KINDS]
    body = "; ".join([*sections, f"s {USAGE} t"])
    return "; ".join(
        [
            "d=$(mktemp -d) || exit 1",
            SECTION,
            LIST,
            sampler(window),
            f"b(){{ {body}; }}",
            f"echo {BEGIN}",
            "b | gzip -c | base64",
            f"echo {END}",
            'rm -rf "$d"',
        ]
    )


def sections_of(logs: str) -> Dict[str, str]:
    """Read the sections out of command invoke's logs: gzip in base64 between the begin and end marks."""
    lines = [line.strip() for line in logs.splitlines()]
    if BEGIN not in lines:
        raise UnexpectedResponse("unexpected answer: command invoke gave no inventory")
    start = lines.index(BEGIN) + 1
    if END not in lines[start:]:
        # Azure keeps at most 512 KB of command invoke's logs, so a larger answer has no end mark.
        raise ClusterUnreadable(TOO_LARGE)
    payload = "".join(lines[start : lines.index(END, start)])
    try:
        text = gzip.decompress(base64.b64decode(payload)).decode("utf-8")
    except (ValueError, OSError, EOFError, zlib.error):
        raise UnexpectedResponse("unexpected answer: command invoke gave an inventory that does not decode") from None
    sections: Dict[str, List[str]] = {}
    current: Optional[List[str]] = None
    for line in text.splitlines():
        if line.startswith(MARK):
            current = sections.setdefault(line[len(MARK) :], [])
        elif current is None:
            raise UnexpectedResponse("unexpected answer: command invoke gave output before its first section")
        else:
            current.append(line)
    return {name: "\n".join(body).strip() for name, body in sections.items()}


def section(sections: Dict[str, str], name: str) -> str:
    """Give one section's text; a missing section is unexpected."""
    if name not in sections:
        raise UnexpectedResponse(f"unexpected answer: command invoke gave no {name} section")
    return sections[name]


def kind_items(kind: Kind, text: str) -> List[Json]:
    """Read one kind's section: its kept items, or the failure it names."""
    if text.startswith("!"):
        raise listing_failure(kind, text[1:])
    items = parse_json(text, "command invoke")
    if not isinstance(items, list):
        raise UnexpectedResponse(f"unexpected answer: command invoke's {kind.name} section is not a list")
    return items


def usage_section(text: str) -> Usage:
    """Read the usage section: the window's peaks, outside the command-invoke pod's namespace, or why there are none."""
    if text.startswith("!"):
        return usage_of([], str(failure(text[1:])))
    table = {key: peak for key, peak in top_table(text).items() if key.namespace != INVOKE_NAMESPACE}
    return usage_of([table], None)


class InvokeKubeApi:
    """A cluster read through az aks command invoke, for an AKS cluster whose API server is private or IP-limited."""

    def __init__(self, azure: AzureCli, cluster: AksCluster) -> None:
        """Read cluster through azure."""
        self._azure = azure
        self._cluster = cluster

    def read(self, window: SampleWindow) -> Snapshot:
        """Read every kind and sample usage over window in one command invoke, given the window plus the reads."""
        logs = self._azure.invoke(self._cluster, script(window), window.seconds + INVOKE_READ_SECONDS)
        sections = sections_of(logs)
        items = {kind.name: kind_items(kind, section(sections, kind.name)) for kind in KINDS}
        read = snapshot(items, usage_section(section(sections, USAGE)))
        return dataclasses.replace(read, pods=tuple(pod for pod in read.pods if pod.namespace != INVOKE_NAMESPACE))
