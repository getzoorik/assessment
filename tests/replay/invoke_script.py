"""The command-invoke script, run on this computer against a made-up cluster.

A stub kubectl serves the cluster's objects as kubectl get -o json gives them, and the cluster's top samples in turn.
The system's own sh, jq, awk, gzip and base64 do the rest, so a test reads what the script itself prints. The script's
PATH holds the stubs and links to those tools only, so no real kubectl is reachable from it.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Mapping, Optional, Sequence

from tests.replay.clusters import Cluster, TopFailure
from zoorik.kubectl.invoke import script
from zoorik.kubectl.objects import KINDS, Json, Kind
from zoorik.kubectl.usage import SampleWindow

SHELL = "/bin/sh"
SYSTEM_PATH = "/usr/bin:/bin"
TOOLS = ("jq", "awk", "gzip", "base64", "mktemp", "head", "tr", "cat", "rm")
SCRIPT_TIMEOUT = 300
# Answers kubectl get from <resource>.json, or fails with <resource>.err; answers the n-th top from top-<n>.txt, or
# fails with top-<n>.err. Every command it gets is written to calls.
STUB_KUBECTL = """#!/bin/sh
echo "kubectl $*" >>"$STUB_DIR/calls"
case "$1" in
get)
  if [ -f "$STUB_DIR/$2.err" ]; then cat "$STUB_DIR/$2.err" >&2; exit 1; fi
  cat "$STUB_DIR/$2.json" ;;
top)
  n=$(cat "$STUB_DIR/count"); n=$((n + 1)); echo "$n" >"$STUB_DIR/count"
  if [ -f "$STUB_DIR/top-$n.err" ]; then cat "$STUB_DIR/top-$n.err" >&2; exit 1; fi
  cat "$STUB_DIR/top-$n.txt" ;;
*)
  echo "unexpected kubectl command: $*" >&2; exit 2 ;;
esac
"""
# Waits no time, and writes the wait to calls.
STUB_SLEEP = """#!/bin/sh
echo "sleep $*" >>"$STUB_DIR/calls"
"""


@dataclass(frozen=True)
class ScriptRun:
    """What the script printed, as command invoke would give it back, and the commands it ran, in order."""

    logs: str
    calls: List[str]


def missing_tools() -> List[str]:
    """Name what this computer lacks to run the script: on Windows, the shell; elsewhere, any tool it uses."""
    if os.name != "posix" or not os.path.exists(SHELL):
        return ["sh"]
    return [tool for tool in TOOLS if shutil.which(tool, path=SYSTEM_PATH) is None]


def listed(kind: Kind, items: Sequence[Json]) -> str:
    """Write a kind's objects as kubectl get -A -o json gives them: a List whose items carry their kind."""
    records = [{"apiVersion": "v1", "kind": kind.item_kind, **item} for item in items]
    return json.dumps({"apiVersion": "v1", "kind": "List", "metadata": {"resourceVersion": ""}, "items": records})


def write(path: str, text: str, mode: int = 0o644) -> None:
    """Write text to a new file at path with mode."""
    with open(path, "w", encoding="utf-8", newline="\n") as file:
        file.write(text)
    os.chmod(path, mode)


def run_script(
    cluster: Cluster, window: SampleWindow, folder: str, failures: Optional[Mapping[str, str]] = None
) -> ScriptRun:
    """Run window's script in folder against cluster; failures gives the error of each kind that fails, by name."""
    failed = failures or {}
    tools, stub, temp = (os.path.join(folder, name) for name in ("bin", "cluster", "tmp"))
    for path in (tools, stub, temp):
        os.makedirs(path)
    for tool in TOOLS:
        found = shutil.which(tool, path=SYSTEM_PATH)
        if found is None:
            raise AssertionError(f"{tool} is not installed")
        os.symlink(found, os.path.join(tools, tool))
    write(os.path.join(tools, "kubectl"), STUB_KUBECTL, 0o755)
    write(os.path.join(tools, "sleep"), STUB_SLEEP, 0o755)
    for kind in KINDS:
        if kind.name in failed:
            write(os.path.join(stub, f"{kind.resource}.err"), failed[kind.name])
        else:
            write(os.path.join(stub, f"{kind.resource}.json"), listed(kind, cluster.objects[kind.name]))
    write(os.path.join(stub, "count"), "0\n")
    write(os.path.join(stub, "calls"), "")
    for number, sample in enumerate(cluster.samples, 1):
        if isinstance(sample, TopFailure):
            write(os.path.join(stub, f"top-{number}.err"), sample.stderr)
        else:
            write(os.path.join(stub, f"top-{number}.txt"), sample)
    completed = subprocess.run(
        [SHELL, "-c", script(window)],
        env={"PATH": tools, "STUB_DIR": stub, "TMPDIR": temp},
        capture_output=True,
        text=True,
        timeout=SCRIPT_TIMEOUT,
        check=False,
    )
    if completed.returncode != 0 or completed.stderr:
        raise AssertionError(f"the script failed with {completed.returncode}: {completed.stderr}")
    with open(os.path.join(stub, "calls"), encoding="utf-8") as file:
        calls = file.read().splitlines()
    return ScriptRun(completed.stdout, calls)
