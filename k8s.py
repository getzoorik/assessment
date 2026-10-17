#!/usr/bin/env python3
"""Zoorik Coral: Kubernetes cost assessment.

Reads your AKS and EKS clusters, estimates what right-sizing could save, and
writes one PDF report for you to e-mail to Zoorik. Nothing to install.

Run it in Azure Cloud Shell (every AKS cluster in the current subscription),
in AWS CloudShell (every EKS cluster in every enabled region), or in any
other terminal on Linux, macOS or Windows, where it uses az or aws if signed
in, else the current kubectl context:
    npx zoorik assess coral
    zoorik assess coral        (after pip install zoorik)
    curl -fsSL https://zoorik.com/assess/k8s | python3 -
The curl line is for bash or zsh: Linux, macOS and both Cloud Shells. On
Windows, use one of the first two, in PowerShell or Command Prompt.

Options go after coral, or after the dash. --context NAME assesses only that
context:
    zoorik assess coral --anonymize
    curl -fsSL https://zoorik.com/assess/k8s | python3 - --context prod --sample-minutes 10

The source is at github.com/getzoorik/assessment.

What it does:
  - It reads. In each cluster it runs only kubectl get and kubectl top. With
    the az and aws CLIs it reads which account you are signed in to, lists
    your clusters, node pools and regions, writes each cluster's credentials
    to a temporary kubeconfig, and looks up AWS prices.
  - For a private AKS cluster kubectl cannot read from here, or an AKS
    cluster kubectl cannot reach or sign in to (kubelogin not installed), it
    uses az aks command invoke: Azure runs the same kubectl reads in a
    short-lived pod of its own, in the cluster's aks-command namespace, and
    records the call in its activity log. --no-invoke skips that; such a
    cluster is then not assessed.
  - It never reads Secrets or ConfigMaps. From what it reads it keeps only
    names, owners, labels for matching, and resource requests and limits:
    never env values, annotations or pod IPs.
  - It sends nothing to Zoorik. Its only calls go to your clusters, your
    cloud's own APIs and the public Azure price list (prices.azure.com),
    which receives a region and a machine type.
  - It writes one file, the PDF report. Its working files (kubectl's cache,
    a kubeconfig for the clusters it finds through az or aws and, on Windows,
    any az argument that cmd.exe would change) go in a temporary folder that
    is deleted when it exits; your own kubeconfig is never changed.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import gzip
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------- config

VERSION = "1.2.0"
ONE_LINER = "curl -fsSL https://zoorik.com/assess/k8s | python3 -"
SEND_TO = "founders@zoorik.com"

HOURS_PER_MONTH = 730
HEADROOM = 1.3
PACKING = 0.85
CPU_FLOOR = 0.010
MEM_FLOOR = 32 * 2**20
SAMPLE_INTERVAL = 30
REQUEST_TIMEOUT = "60s"
PAGE_SIZE = 500
INVOKE_TIMEOUT = 900

GIB = 2**30
DOCS = "see github.com/getzoorik/assessment#access-it-needs"
UNREACHABLE = "API server not reachable from here"
PRIVATE = "private API not reachable from here"
NETWORK = {"aks": "VNet", "eks": "VPC"}
NO_ACCESS = ("access denied", "this identity has no access to the cluster")
INVOKE_WHEN = (UNREACHABLE, "kubectl timed out", "kubelogin is not installed")
OVERHEAD_KINDS = ("DaemonSet", "Static pod")
POOL_LABELS = ("kubernetes.azure.com/agentpool", "agentpool", "eks.amazonaws.com/nodegroup", "karpenter.sh/nodepool")
AWS_ENV = dict(os.environ, AWS_PAGER="")
WORKDIR = ""


def say(text: str = "", end: str = "\n") -> None:
    print(text, end=end, flush=True)


# ---------------------------------------------------------------- commands

class Failed(Exception):
    """An expected failure, carried as a short reason."""


_REASONS = (
    ("failed to connect to msi", "Cloud Shell sign-in issue; run az login, then run this again"),
    ("executable kubelogin not found", "kubelogin is not installed"),
    ("aadsts", "Entra ID sign-in failed; run az login, then run this again"),
    ("metrics api not available", "metrics-server is not installed"),
    ("forbidden", "access denied"),
    ("authorizationfailed", "access denied"),
    ("accessdenied", "access denied"),
    ("unauthorizedoperation", "access denied"),
    ("metrics.k8s.io", "metrics-server is not installed"),
    ("unauthorized", "this identity has no access to the cluster"),
    ("must be logged in", "this identity has no access to the cluster"),
    ("az login", "not signed in to Azure; run az login"),
    ("unable to locate credentials", "no AWS credentials"),
    ("expiredtoken", "AWS credentials have expired"),
    ("i/o timeout", UNREACHABLE),
    ("dial tcp", UNREACHABLE),
    ("no such host", UNREACHABLE),
    ("connection refused", UNREACHABLE),
    ("was refused", UNREACHABLE),
    ("no route to host", UNREACHABLE),
    ("network is unreachable", UNREACHABLE),
    ("unable to connect", UNREACHABLE),
    ("deadline exceeded", UNREACHABLE),
    ("timeout exceeded", UNREACHABLE),
    ("tls handshake timeout", UNREACHABLE),
    ("timed out", UNREACHABLE),
)


def reason(text: str) -> str:
    """Turn tool error output into a short reason, free of user names."""
    low = (text or "").lower()
    for needle, short in _REASONS:
        if needle in low:
            return short
    line = next((x.strip() for x in (text or "").splitlines() if x.strip()), "unknown error")
    line = re.sub(r"^(error|ERROR|Error)[:\s]+", "", line)
    line = re.sub(r"\"[^\"]*\"|'[^']*'", "...", line)
    return line[:100]


CMD_SPECIAL = re.compile(r'["&|<>^%()\r\n]')


def decoded(data: bytes) -> str:
    """CLI output as text, with Windows line ends made plain: UTF-8, or on Windows the console code page, which az
    writes in when its output is piped."""
    try:
        out = data.decode("utf-8")
    except UnicodeDecodeError:
        out = data.decode("mbcs" if os.name == "nt" else "utf-8", "replace")
    return out.replace("\r\n", "\n")


def arg_file(value: str) -> str:
    """On Windows az is az.cmd, whose arguments cmd.exe reads again, so a value it would change goes in a file
    that az reads instead (@path). The file is in WORKDIR, deleted at exit."""
    fd, path = tempfile.mkstemp(dir=WORKDIR)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(value)
    return "@" + path


def run(cmd: list[str], timeout: float = 120, env: Optional[dict] = None) -> str:
    """Run one command and return its output, or raise Failed. The command is found on PATH as the shell finds it,
    so az.cmd on Windows works too.
    The Azure CLI's own usage telemetry is switched off for these calls only; your az settings stay as they are."""
    if cmd and cmd[0] == "az":
        env = dict(env or os.environ, AZURE_CORE_COLLECT_TELEMETRY="false")
        if os.name == "nt":
            cmd = cmd[:1] + [arg_file(a) if CMD_SPECIAL.search(a) else a for a in cmd[1:]]
    try:
        p = subprocess.run([shutil.which(cmd[0]) or cmd[0], *cmd[1:]], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
    except FileNotFoundError:
        raise Failed(f"{cmd[0]} is not installed") from None
    except subprocess.TimeoutExpired:
        raise Failed(f"{cmd[0]} timed out") from None
    out = decoded(p.stdout)
    if p.returncode != 0:
        raise Failed(reason(decoded(p.stderr) or out))
    return out


def az(*args: str, timeout: float = 120):
    out = run(["az", *args, "-o", "json", "--only-show-errors"], timeout)
    try:
        return json.loads(out or "null")
    except ValueError:
        raise Failed("unexpected az output") from None


def aws(*args: str, region: str = "", timeout: float = 60):
    cmd = ["aws", *args, "--output", "json"] + (["--region", region] if region else [])
    out = run(cmd, timeout, env=AWS_ENV)
    try:
        return json.loads(out or "null")
    except ValueError:
        raise Failed("unexpected aws output") from None


def works(cmd: list[str]) -> bool:
    if not shutil.which(cmd[0]):
        return False
    try:
        run(cmd, 30, env=AWS_ENV)
        return True
    except Failed:
        return False


def http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": f"zoorik-k8s-assessment/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        raise Failed("price list not reachable") from None


# ---------------------------------------------------------------- quantities

_UNITS = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
          "P": 1e15, "E": 1e18, "Ki": 2.0**10, "Mi": 2.0**20, "Gi": 2.0**30, "Ti": 2.0**40, "Pi": 2.0**50,
          "Ei": 2.0**60}


_QUANTITY = re.compile(r"\s*((?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)\s*")


def is_quantity(value) -> bool:
    m = _QUANTITY.fullmatch(str(value or ""))
    return bool(m) and m.group(2) in _UNITS


def quantity(value) -> float:
    """Parse a Kubernetes quantity (CPU cores or bytes); 0 when it does not parse."""
    if not is_quantity(value):
        return 0.0
    m = _QUANTITY.fullmatch(str(value))
    return float(m.group(1)) * _UNITS[m.group(2)]


# ---------------------------------------------------------------- model

@dataclass
class Pool:
    """A node pool as the cloud API reports it."""
    name: str
    instance_type: str
    count: int
    capacity: str
    os: str
    minimum: Optional[int]


@dataclass
class Target:
    """One cluster to assess and how to reach it."""
    name: str
    source: str = "context"
    cloud: str = ""
    region: str = ""
    context: str = ""
    kubeconfig: str = ""
    subscription: str = ""
    resource_group: str = ""
    private: bool = False
    pools: list = field(default_factory=list)
    skip: str = ""


@dataclass
class Node:
    name: str
    pool: str
    instance_type: str
    capacity: str
    os: str
    region: str
    cloud: str
    cpu_alloc: float
    mem_alloc: float
    pods_alloc: int
    provider: str = ""


@dataclass
class Container:
    name: str
    cpu_req: float
    mem_req: float
    sidecar: bool = False
    cpu_use: Optional[float] = None
    mem_use: Optional[float] = None

    def requests(self, recommended: bool) -> tuple:
        if not recommended:
            return self.cpu_req, self.mem_req
        return advise(self.cpu_req, self.cpu_use, CPU_FLOOR), advise(self.mem_req, self.mem_use, MEM_FLOOR)


@dataclass
class Pod:
    namespace: str
    name: str
    node: str
    kind: str
    owner: str
    containers: list
    inits: list
    overhead: tuple
    pod_req: tuple = (None, None)

    def request(self, recommended: bool = False) -> tuple:
        """Effective pod request as the scheduler counts it, plus overhead.

        A pod-level request wins and is kept as it is; otherwise it is
        max(running set, init phase), right-sized when recommended is set.
        """
        total = []
        for i in (0, 1):
            if self.pod_req[i] is not None:
                total.append(self.pod_req[i] + self.overhead[i])
                continue
            sidecars, init_peak = 0.0, 0.0
            for c in self.inits:
                if c.sidecar:
                    sidecars += c.requests(recommended)[i]
                    init_peak = max(init_peak, sidecars)
                else:
                    init_peak = max(init_peak, sidecars + c.requests(False)[i])
            running = sum(c.requests(recommended)[i] for c in self.containers) + sidecars
            total.append(max(running, init_peak) + self.overhead[i])
        return total[0], total[1]

    def running(self) -> list:
        return self.containers + [c for c in self.inits if c.sidecar]


@dataclass
class Workload:
    namespace: str
    kind: str
    name: str
    pods: list = field(default_factory=list)


@dataclass
class PoolRow:
    """A node pool's machines of one type: today, needed after right-sizing, and the hourly price or why none."""
    pool: str
    instance_type: str
    capacity: str
    nodes: int
    needed: Optional[int] = None
    price: Optional[float] = None
    note: str = ""

    def monthly(self, nodes: int) -> float:
        return nodes * self.price * HOURS_PER_MONTH


@dataclass
class Cluster:
    target: Target
    cloud: str = ""
    region: str = ""
    failed: str = ""
    nodes: list = field(default_factory=list)
    pods: list = field(default_factory=list)
    workloads: list = field(default_factory=list)
    pools: list = field(default_factory=list)
    ctr_use: dict = field(default_factory=dict)
    samples: int = 0
    snapshot: bool = False
    metrics_note: str = ""


def advise(request: float, used: Optional[float], floor: float) -> float:
    """Recommended request: peak use x headroom, floored, never above today."""
    if used is None:
        return request
    recommended = max(used * HEADROOM, floor)
    return recommended if request == 0 else min(recommended, request)


# ---------------------------------------------------------------- kubectl

NODE_JQ = ('[.items[]|{metadata:{name:.metadata.name,labels:((.metadata.labels//{})|with_entries(select(.key|'
           'test("instance-type|agentpool|nodegroup|nodepool|capacity|scalesetpriority|topology.kubernetes.io/region|'
           'kubernetes.io/os"))))},spec:{providerID:.spec.providerID},status:{allocatable:.status.allocatable}}]')
POD_JQ = ('[.items[]|select(.status.phase!="Succeeded" and .status.phase!="Failed")|{metadata:{name:.metadata.name,'
          'namespace:.metadata.namespace,labels:((.metadata.labels//{})|with_entries(select(.key=='
          '"pod-template-hash"))),ownerReferences:[.metadata.ownerReferences[]?|{kind,name,controller}]},'
          'spec:{nodeName:.spec.nodeName,overhead:.spec.overhead,resources:.spec.resources,'
          'containers:[.spec.containers[]|{name,resources}],initContainers:[.spec.initContainers[]?|'
          '{name,resources,restartPolicy}]}}]')
OWNED_JQ = ('[.items[]|select(.metadata.ownerReferences)|{metadata:{name:.metadata.name,'
            'namespace:.metadata.namespace,ownerReferences:[.metadata.ownerReferences[]|{kind,name,controller}]}}]')

RESOURCES = {
    "nodes": ("nodes", "/api/v1/nodes", NODE_JQ),
    "pods": ("pods", "/api/v1/pods", POD_JQ),
    "replicasets": ("replicasets.apps", "/apis/apps/v1/replicasets", OWNED_JQ),
    "jobs": ("jobs.batch", "/apis/batch/v1/jobs", OWNED_JQ),
}


def slim_pod(o: dict) -> Optional[dict]:
    """Keep what POD_JQ keeps; drop finished pods, env values and everything else."""
    md, spec, phase = o.get("metadata") or {}, o.get("spec") or {}, (o.get("status") or {}).get("phase")
    if phase in ("Succeeded", "Failed"):
        return None

    def keep(items, *keys):
        return [{k: x.get(k) for k in keys} for x in items or []]

    labels = {k: v for k, v in (md.get("labels") or {}).items() if k == "pod-template-hash"}
    return {"metadata": {"name": md.get("name"), "namespace": md.get("namespace"), "labels": labels,
                         "ownerReferences": md.get("ownerReferences")},
            "spec": {"nodeName": spec.get("nodeName"), "overhead": spec.get("overhead"),
                     "resources": spec.get("resources"),
                     "containers": keep(spec.get("containers"), "name", "resources"),
                     "initContainers": keep(spec.get("initContainers"), "name", "resources", "restartPolicy")}}


def slim_owned(o: dict) -> Optional[dict]:
    """Keep what OWNED_JQ keeps: name, namespace and owners; drop the pod template."""
    md = o.get("metadata") or {}
    if not md.get("ownerReferences"):
        return None
    return {"metadata": {k: md.get(k) for k in ("name", "namespace", "ownerReferences")}}


SLIM = {"pods": slim_pod, "replicasets": slim_owned, "jobs": slim_owned}


def kubectl(t: Target, *args: str, timeout: float = 300, request_timeout: str = REQUEST_TIMEOUT) -> str:
    cmd = ["kubectl", "--cache-dir", os.path.join(WORKDIR, "cache"), "--request-timeout", request_timeout]
    if t.kubeconfig:
        cmd += ["--kubeconfig", t.kubeconfig]
    if t.context:
        cmd += ["--context", t.context]
    return run(cmd + list(args), timeout)


def list_paged(t: Target, kind: str) -> list:
    """List one kind page by page, keeping only the fields the report needs."""
    path, slim = RESOURCES[kind][1], SLIM.get(kind)
    items, token = [], ""
    while True:
        query = {"limit": PAGE_SIZE, **({"continue": token} if token else {})}
        out = kubectl(t, "get", "--raw", f"{path}?{urllib.parse.urlencode(query)}", timeout=120)
        try:
            page = json.loads(out)
        except ValueError:
            raise Failed("unexpected kubectl output") from None
        page_items = page.get("items") or []
        items += [x for x in map(slim, page_items) if x] if slim else page_items
        token = (page.get("metadata") or {}).get("continue")
        if not token:
            return items


def fetch_direct(t: Target) -> dict:
    """Check the API server answers, then read every resource kind with kubectl get."""
    kubectl(t, "get", "--raw", "/version", timeout=60, request_timeout="20s")
    snap = {"errors": {}}
    with ThreadPoolExecutor(4) as ex:
        futures = {kind: ex.submit(list_paged, t, kind) for kind in RESOURCES}
    for kind, fut in futures.items():
        try:
            snap[kind] = fut.result()
        except Failed as e:
            snap[kind], snap["errors"][kind] = [], str(e)
    return snap


def top_direct(t: Target) -> dict:
    pods = parse_top_pods(kubectl(t, "top", "pods", "-A", "--containers", "--no-headers", timeout=180))
    if not pods:
        raise Failed("metrics-server returned no data")
    return pods


def parse_top_pods(text: str) -> dict:
    """NAMESPACE POD CONTAINER CPU MEMORY; lines that do not parse are skipped."""
    check_top(text)
    out = {}
    for line in text.splitlines():
        cols = line.split()
        if len(cols) >= 5 and is_quantity(cols[3]) and is_quantity(cols[4]):
            out[(cols[0], cols[1], cols[2])] = (quantity(cols[3]), quantity(cols[4]))
    return out


def check_top(text: str) -> None:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("!"):
            raise Failed(reason(line[1:]))
        if line.lower().startswith("error"):
            raise Failed(reason(line))


# ---------------------------------------------------------------- command invoke

_HELPERS = (
    "E=$(mktemp 2>/dev/null || echo /dev/null); "
    "r(){ o=$(\"$@\" 2>\"$E\") || { printf '!%s\\n' \"$(head -c 300 \"$E\" | tr '\\n' ' ')\"; return 1; }; }; "
    "p(){ r \"$@\" && printf '%s\\n' \"$o\"; }; "
    "g(){ r kubectl get \"$1\" -A -o json --request-timeout=60s --chunk-size=500 && "
    "{ printf '%s' \"$o\" | jq -c \"$2\" 2>/dev/null || echo '!unreadable output'; }; }")
_SECTIONS = {
    "version": "p kubectl get --raw /version --request-timeout=60s",
    "top_pods": "p kubectl top pods -A --containers --no-headers --request-timeout=60s",
}
_SECTIONS.update({kind: f"g {res} '{jq}'" for kind, (res, _, jq) in RESOURCES.items()})


class Truncated(Exception):
    """The invoke output hit Azure's size limit."""


def bundle_script(sections: list) -> str:
    """One shell line that prints the sections, gzip+base64 when available.

    kubectl's stderr never enters the data: a failed command prints one line
    starting with '!' and the start of its error instead.
    """
    body = "; ".join(f"echo @@{name}; {_SECTIONS[name]}" for name in sections)
    return (f"{_HELPERS}; b(){{ {body}; echo @@end; }}; "
            "if command -v gzip >/dev/null 2>&1 && command -v base64 >/dev/null 2>&1; "
            "then echo @@gz; b | gzip -c | base64; echo @@gzend; else b; fi; "
            "[ \"$E\" = /dev/null ] || rm -f \"$E\"")


def parse_bundle(logs: str) -> dict:
    if "@@gz\n" in logs:
        body = logs.split("@@gz\n", 1)[1]
        if "@@gzend" not in body:
            raise Truncated()
        try:
            logs = gzip.decompress(base64.b64decode(body.split("@@gzend", 1)[0])).decode("utf-8", "replace")
        except (binascii.Error, OSError, EOFError, zlib.error):
            raise Truncated() from None
    sections, name = {}, None
    for line in logs.splitlines():
        if line.startswith("@@"):
            name = line[2:].strip()
            sections[name] = []
        elif name:
            sections[name].append(line)
    if "end" not in sections:
        raise Truncated()
    return {k: "\n".join(v) for k, v in sections.items() if k != "end"}


def az_invoke(t: Target, script: str) -> str:
    out = run(["az", "aks", "command", "invoke", "--subscription", t.subscription, "-g", t.resource_group,
               "-n", t.name, "--command", script, "-o", "json", "--only-show-errors"], INVOKE_TIMEOUT)
    try:
        data = json.loads(out)
    except ValueError:
        raise Failed("unexpected command invoke output") from None
    if data.get("provisioningState") not in (None, "Succeeded"):
        raise Failed(reason(data.get("reason") or "command invoke failed"))
    return data.get("logs") or ""


def fetch_invoke(t: Target) -> dict:
    """Read the cluster through az aks command invoke: one call, or one per kind if too large."""
    names = ["version", *RESOURCES, "top_pods"]
    failed = {}
    try:
        sections = parse_bundle(az_invoke(t, bundle_script(names)))
    except Truncated:
        say("output over Azure's 512 KB limit, reading kind by kind ... ", end="")
        sections = {}
        for name in names:
            try:
                sections.update(parse_bundle(az_invoke(t, bundle_script([name]))))
            except Truncated:
                failed[name] = "too large for command invoke (512 KB limit)"
            except Failed as e:
                failed[name] = str(e)
    texts = {}
    for name in names:
        text = sections.get(name, "").strip()
        if name in failed:
            continue
        if text.startswith("!"):
            failed[name] = reason(text[1:])
        elif not text:
            failed[name] = "no output"
        else:
            texts[name] = text
    if "version" in failed:
        raise Failed(failed["version"])
    snap = {"errors": {}, "top_pods": texts.get("top_pods", ""), "top_error": failed.get("top_pods")}
    for kind in RESOURCES:
        if kind in failed:
            snap[kind], snap["errors"][kind] = [], failed[kind]
            continue
        try:
            snap[kind] = json.loads(texts[kind])
        except ValueError:
            snap[kind], snap["errors"][kind] = [], "unreadable output"
    snap["pods"] = [p for p in snap["pods"] if (p.get("metadata") or {}).get("namespace") != "aks-command"]
    return snap


# ---------------------------------------------------------------- discovery

def in_azure_cloud_shell() -> bool:
    return bool(os.environ.get("ACC_CLOUD")) or os.environ.get("AZUREPS_HOST_ENVIRONMENT", "").startswith("cloud-shell")


def in_aws_cloudshell() -> bool:
    return os.environ.get("AWS_EXECUTION_ENV", "") == "CloudShell"


def discover_azure(subscription: str) -> list:
    acct = az("account", "show", *(["--subscription", subscription] if subscription else []))
    sub = acct.get("id", "")
    targets = []
    for c in az("aks", "list", "--subscription", sub, timeout=180) or []:
        access = c.get("apiServerAccessProfile") or {}
        t = Target(name=c.get("name", ""), source="aks", cloud="azure", region=c.get("location", ""),
                   subscription=sub, resource_group=c.get("resourceGroup", ""),
                   private=bool(access.get("enablePrivateCluster")))
        for p in c.get("agentPoolProfiles") or []:
            t.pools.append(Pool(name=p.get("name", ""), instance_type=p.get("vmSize", ""), count=p.get("count") or 0,
                                capacity="spot" if p.get("scaleSetPriority") == "Spot" else "on-demand",
                                os=(p.get("osType") or "linux").lower(),
                                minimum=p.get("minCount") if p.get("enableAutoScaling") else None))
        if (c.get("powerState") or {}).get("code") == "Stopped":
            t.skip = "cluster is stopped"
        targets.append(t)
    say(f"Azure subscription {acct.get('name', '')} ({sub}): {len(targets)} AKS cluster(s)")
    return targets


def discover_aws(regions: list) -> list:
    ident = aws("sts", "get-caller-identity")
    if not regions:
        home = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        try:
            regions = aws("ec2", "describe-regions", "--query", "Regions[].RegionName", region=home) or [home]
        except Failed as e:
            say(f"AWS: could not list regions ({e}); searching {home} only (add --region NAME for others)")
            regions = [home]
    errors = {}

    def in_region(region: str) -> list:
        try:
            return [(region, n) for n in (aws("eks", "list-clusters", region=region) or {}).get("clusters", [])]
        except Failed as e:
            errors[region] = str(e)
            return []

    with ThreadPoolExecutor(8) as ex:
        found = [x for xs in ex.map(in_region, sorted(regions)) for x in xs]
        if len(errors) == len(regions):
            raise Failed(errors[min(errors)])
        targets = list(ex.map(lambda rn: describe_eks(*rn), found))
    say(f"AWS account {ident.get('Account', '')}: {len(targets)} EKS cluster(s) in "
        f"{len(regions) - len(errors)} region(s)")
    if errors:
        say(f"  could not search {', '.join(sorted(errors))}: {'; '.join(sorted(set(errors.values())))}")
    return targets


def describe_eks(region: str, name: str) -> Target:
    t = Target(name=name, source="eks", cloud="aws", region=region)
    try:
        c = aws("eks", "describe-cluster", "--name", name, region=region)["cluster"]
    except (Failed, KeyError, TypeError) as e:
        t.skip = str(e) if isinstance(e, Failed) else "unexpected describe-cluster output"
        return t
    t.private = not (c.get("resourcesVpcConfig") or {}).get("endpointPublicAccess", True)
    if c.get("status") != "ACTIVE":
        t.skip = f"cluster status is {c.get('status')}"
    try:
        for g in (aws("eks", "list-nodegroups", "--cluster-name", name, region=region) or {}).get("nodegroups", []):
            ng = aws("eks", "describe-nodegroup", "--cluster-name", name, "--nodegroup-name", g,
                     region=region)["nodegroup"]
            sc, ami = ng.get("scalingConfig") or {}, ng.get("amiType", "")
            t.pools.append(Pool(name=g, instance_type=",".join(ng.get("instanceTypes") or []),
                                count=sc.get("desiredSize") or 0,
                                capacity={"SPOT": "spot", "ON_DEMAND": "on-demand"}.get(ng.get("capacityType"),
                                                                                       "unknown"),
                                os="" if ami == "CUSTOM" else "windows" if "WINDOWS" in ami else "linux",
                                minimum=sc.get("minSize")))
    except (Failed, KeyError, TypeError):
        t.pools = []
    return t


def credentials(t: Target) -> None:
    """Write this cluster's credentials to a temporary kubeconfig."""
    path = os.path.join(WORKDIR, re.sub(r"[^A-Za-z0-9_.-]", "_", f"{t.source}-{t.region}-{t.resource_group}-{t.name}"))
    if t.source == "aks":
        try:
            run(["az", "aks", "get-credentials", "--subscription", t.subscription, "-g", t.resource_group,
                 "-n", t.name, "--file", path, "--overwrite-existing", "--only-show-errors"], 120)
        except Failed as e:
            hint = f" (it needs the Azure Kubernetes Service Cluster User Role; {DOCS})" if str(e) in NO_ACCESS else ""
            raise Failed(f"{e}{hint}") from None
        with open(path, encoding="utf-8") as f:
            needs_kubelogin = "kubelogin" in f.read()
        if needs_kubelogin:
            if not shutil.which("kubelogin"):
                raise Failed("kubelogin is not installed")
            run(["kubelogin", "convert-kubeconfig", "-l", "azurecli", "--kubeconfig", path], 60)
    else:
        run(["aws", "eks", "update-kubeconfig", "--name", t.name, "--region", t.region, "--kubeconfig", path],
            120, env=AWS_ENV)
    t.kubeconfig = path


def kube_contexts(all_contexts: bool) -> list:
    if all_contexts:
        names = run(["kubectl", "config", "get-contexts", "-o", "name"], 30).split()
    else:
        names = [run(["kubectl", "config", "current-context"], 30).strip()]
    if not any(names):
        raise Failed("no kube context is set")
    return [Target(name=n, context=n) for n in names if n]


def choose_targets(args, shell: str) -> tuple:
    """Return (targets, whether a cloud search failed, why kubectl found none) from the flags or where it runs."""
    discover = list(args.discover or [])
    if args.subscription and "azure" not in discover:
        discover.append("azure")
    if args.region and "aws" not in discover:
        discover.append("aws")
    explicit = bool(args.context or args.all_contexts or discover)
    if not explicit:
        if shell == "azure" or (not shell and works(["az", "account", "show", "-o", "none"])):
            discover.append("azure")
        if shell == "aws" or (not shell and works(["aws", "sts", "get-caller-identity"])):
            discover.append("aws")
    targets, search_failed, why = [], False, ""
    for cloud in discover:
        try:
            targets += discover_azure(args.subscription or "") if cloud == "azure" else discover_aws(args.region or [])
        except Failed as e:
            search_failed = True
            say(f"{'Azure' if cloud == 'azure' else 'AWS'}: could not list clusters: {e}")
    try:
        if args.all_contexts:
            targets += kube_contexts(True)
        targets += [Target(name=c, context=c) for c in args.context or []]
        if not explicit and not targets:
            current = kube_contexts(False)
            kubectl(current[0], "get", "--raw", "/version", timeout=60, request_timeout="20s")
            targets = current
    except Failed as e:
        why = f"kubectl: {e}"
    seen, unique = set(), []
    for t in targets:
        key = (t.source, t.context, t.subscription, t.resource_group, t.region, t.name)
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique, search_failed, why


# ---------------------------------------------------------------- collectors

def collect(t: Target, allow_invoke: bool) -> Cluster:
    """Inventory one cluster; record the reason and go on when it fails."""
    c = Cluster(target=t, cloud=t.cloud, region=t.region)
    if t.skip:
        c.failed = t.skip
        return c
    try:
        if t.source in ("aks", "eks"):
            credentials(t)
        build(c, fetch_direct(t))
        return c
    except Failed as e:
        error = str(e)
    if t.private and error in (UNREACHABLE, "kubectl timed out"):
        error = PRIVATE
    if t.source == "aks" and allow_invoke and (t.private or error in INVOKE_WHEN):
        try:
            say("via az aks command invoke (about a minute) ... ", end="")
            snap = fetch_invoke(t)
            build(c, snap)
            c.snapshot = True
            try:
                if snap["top_error"]:
                    raise Failed(snap["top_error"])
                c.ctr_use = parse_top_pods(snap["top_pods"])
                c.samples = 1 if c.ctr_use else 0
                c.metrics_note = "" if c.ctr_use else "metrics-server returned no data"
            except Failed as e:
                c.ctr_use, c.metrics_note = {}, str(e)
            return c
        except Failed as e:
            c.failed = f"{error}; command invoke: {e}"
    elif error == PRIVATE and t.source in NETWORK:
        c.failed = f"{PRIVATE}; run from a machine inside the {NETWORK[t.source]}"
    elif error in NO_ACCESS:
        c.failed = f"{error} ({'it needs an EKS access entry; ' if t.source == 'eks' else ''}{DOCS})"
    else:
        c.failed = error
    return c


def build(c: Cluster, snap: dict) -> None:
    """Turn raw objects into nodes, pods and workloads."""
    errors = snap["errors"]
    for kind in ("nodes", "pods"):
        if kind in errors:
            denied = errors[kind] in NO_ACCESS
            hint = f" (it needs cluster-wide read access to nodes and pods; {DOCS})" if denied else ""
            raise Failed(f"cannot list {kind}: {errors[kind]}{hint}")
    c.nodes = [make_node(o) for o in snap["nodes"]]
    hints = Counter(n.cloud for n in c.nodes if n.cloud)
    c.cloud = hints.most_common(1)[0][0] if hints else c.cloud or "other"
    regions = Counter(n.region for n in c.nodes if n.region)
    c.region = regions.most_common(1)[0][0] if regions else c.region

    owners = {}
    for kind, items in (("ReplicaSet", snap["replicasets"]), ("Job", snap["jobs"])):
        for o in items:
            md = o.get("metadata") or {}
            ref = controller_ref(md)
            if ref:
                owners[(kind, md.get("namespace"), md.get("name"))] = (ref.get("kind", ""), ref.get("name", ""))
    known_nodes = {n.name for n in c.nodes}
    for o in snap["pods"]:
        pod = make_pod(o, owners)
        if pod.node in known_nodes:
            c.pods.append(pod)
    groups = {}
    for p in c.pods:
        w = groups.setdefault((p.namespace, p.kind, p.owner), Workload(p.namespace, p.kind, p.owner))
        w.pods.append(p)
    c.workloads = sorted(groups.values(), key=lambda w: (w.namespace, w.kind, w.name))


def make_node(o: dict) -> Node:
    md, alloc = o.get("metadata") or {}, (o.get("status") or {}).get("allocatable") or {}
    labels = md.get("labels") or {}
    provider = (o.get("spec") or {}).get("providerID") or ""
    if provider.startswith("azure://") or any(k.startswith("kubernetes.azure.com/") for k in labels):
        cloud = "azure"
    elif provider.startswith("aws://") or any(k.startswith("eks.amazonaws.com/") for k in labels):
        cloud = "aws"
    else:
        cloud = ""
    aws_capacity = (labels.get("karpenter.sh/capacity-type") or labels.get("eks.amazonaws.com/capacityType") or "")
    aws_capacity = aws_capacity.lower().replace("_", "-")
    if labels.get("kubernetes.azure.com/scalesetpriority", "").lower() == "spot":
        capacity = "spot"
    elif aws_capacity in ("spot", "on-demand"):
        capacity = aws_capacity
    elif any(k.startswith("kubernetes.azure.com/") for k in labels):
        capacity = "on-demand"
    else:
        capacity = "unknown"
    return Node(
        name=md.get("name", ""),
        pool=next((labels[k] for k in POOL_LABELS if labels.get(k)), "unlabelled"),
        instance_type=(labels.get("node.kubernetes.io/instance-type")
                       or labels.get("beta.kubernetes.io/instance-type", "")),
        capacity=capacity,
        os=labels.get("kubernetes.io/os", ""),
        region=labels.get("topology.kubernetes.io/region", ""),
        cloud=cloud,
        cpu_alloc=quantity(alloc.get("cpu")), mem_alloc=quantity(alloc.get("memory")),
        pods_alloc=int(quantity(alloc.get("pods"))),
        provider=provider)


def controller_ref(md: dict) -> Optional[dict]:
    refs = md.get("ownerReferences") or []
    return next((r for r in refs if r.get("controller")), refs[0] if refs else None)


def make_container(o: dict, sidecar: bool = False) -> Container:
    res = o.get("resources") or {}
    req, lim = res.get("requests") or {}, res.get("limits") or {}
    return Container(name=o.get("name", ""),
                     cpu_req=quantity(req.get("cpu", lim.get("cpu"))),
                     mem_req=quantity(req.get("memory", lim.get("memory"))),
                     sidecar=sidecar)


def make_pod(o: dict, owners: dict) -> Pod:
    md, spec = o.get("metadata") or {}, o.get("spec") or {}
    ns, name, node = md.get("namespace", ""), md.get("name", ""), spec.get("nodeName") or ""
    labels = md.get("labels") or {}
    template_hash = labels.get("pod-template-hash") or ""
    kind, owner = "Pod", name
    ref = controller_ref(md)
    if ref:
        kind, owner = ref.get("kind", ""), ref.get("name", "")
        if (kind, ns, owner) in owners:
            kind, owner = owners[(kind, ns, owner)]
        elif kind == "ReplicaSet" and template_hash and owner.endswith("-" + template_hash):
            kind, owner = "Deployment", owner[:-len(template_hash) - 1]
        elif kind == "Node":
            kind, owner = "Static pod", name[:-len(node) - 1] if node and name.endswith("-" + node) else name
    overhead = spec.get("overhead") or {}
    own_req = (spec.get("resources") or {}).get("requests") or {}
    return Pod(namespace=ns, name=name, node=node, kind=kind, owner=owner,
               containers=[make_container(x) for x in spec.get("containers") or []],
               inits=[make_container(x, x.get("restartPolicy") == "Always") for x in spec.get("initContainers") or []],
               overhead=(quantity(overhead.get("cpu")), quantity(overhead.get("memory"))),
               pod_req=tuple(quantity(own_req[r]) if r in own_req else None for r in ("cpu", "memory")))


def sample(c: Cluster) -> None:
    """Take one kubectl top sample and keep the peak per container."""
    try:
        ctrs = top_direct(c.target)
    except Failed as e:
        if not c.samples:
            c.metrics_note = str(e)
        return
    for key, (cpu, mem) in ctrs.items():
        old = c.ctr_use.get(key, (0.0, 0.0))
        c.ctr_use[key] = (max(old[0], cpu), max(old[1], mem))
    c.samples += 1
    c.metrics_note = ""


def sample_all(clusters: list, minutes: float) -> None:
    live = [c for c in clusters if not c.failed and not c.snapshot]
    if not live:
        return
    rounds = int(minutes * 60 // SAMPLE_INTERVAL) + 1
    if rounds > 1:
        say(f"Sampling usage for {minutes:g} min ({rounds} samples per cluster) ...")
    start = time.monotonic()
    with ThreadPoolExecutor(min(8, len(live))) as ex:
        for i in range(rounds):
            due = [c for c in live if c.samples or i == 0]
            if not due:
                return
            time.sleep(max(0.0, start + i * SAMPLE_INTERVAL - time.monotonic()))
            list(ex.map(sample, due))


def apply_usage(c: Cluster) -> None:
    for p in c.pods:
        for ctr in p.running():
            use = c.ctr_use.get((p.namespace, p.name, ctr.name))
            if use:
                ctr.cpu_use, ctr.mem_use = use


# ---------------------------------------------------------------- pricing

class Pricer:
    """Hourly list prices per machine type, cached, never guessed."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.cache = {}

    def hourly(self, cloud: str, region: str, sku: str, capacity: str, os_name: str) -> tuple:
        """(price, "") or (None, why there is none)."""
        if not self.enabled:
            return None, "pricing skipped (--no-prices)"
        if cloud not in ("azure", "aws"):
            return None, "no public price list for this platform"
        if not sku:
            return None, "instance type unknown"
        if "," in sku:
            return None, "several instance types in one node group"
        if capacity not in ("spot", "on-demand"):
            return None, "Spot or on-demand not known from the node labels"
        if os_name and os_name != "linux":
            return None, f"{os_name} nodes are not priced"
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", region or "") or not re.fullmatch(r"[A-Za-z0-9_.-]+", sku):
            return None, "region unknown"
        key = (cloud, region, sku, capacity)
        if key not in self.cache:
            try:
                fetch = azure_price if cloud == "azure" else aws_price
                self.cache[key] = fetch(region, sku, capacity == "spot"), ""
            except Failed as e:
                self.cache[key] = (None, str(e) if str(e).startswith("no ") else f"price lookup: {e}")
            except (ValueError, KeyError, TypeError, AttributeError):
                self.cache[key] = (None, "price lookup failed")
        return self.cache[key]


def azure_price(region: str, sku: str, spot: bool) -> float:
    query = (f"armRegionName eq '{region}' and armSkuName eq '{sku}' "
             "and serviceName eq 'Virtual Machines' and priceType eq 'Consumption'")
    url = "https://prices.azure.com/api/retail/prices?" + urllib.parse.urlencode({"$filter": query})
    prices = []
    while url:
        data = http_json(url)
        for item in data.get("Items") or []:
            meter, product = item.get("meterName", ""), item.get("productName", "")
            if "Windows" in product or "Low Priority" in meter or item.get("unitOfMeasure") != "1 Hour":
                continue
            if meter.endswith("Spot") == spot and (item.get("unitPrice") or 0) > 0:
                prices.append(float(item["unitPrice"]))
        url = data.get("NextPageLink")
    if not prices:
        raise Failed(f"no list price for {sku} in {region}")
    return min(prices)


def aws_price(region: str, itype: str, spot: bool) -> float:
    if spot:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = aws("ec2", "describe-spot-price-history", "--instance-types", itype,
                  "--product-descriptions", "Linux/UNIX", "--start-time", now, region=region) or {}
        latest = {}
        for h in sorted(out.get("SpotPriceHistory") or [], key=lambda h: h.get("Timestamp", "")):
            latest[h.get("AvailabilityZone")] = float(h.get("SpotPrice") or 0)
        if not latest:
            raise Failed(f"no Spot price for {itype} in {region}")
        return sum(latest.values()) / len(latest)
    terms = (("instanceType", itype), ("regionCode", region), ("operatingSystem", "Linux"), ("tenancy", "Shared"),
             ("preInstalledSw", "NA"), ("capacitystatus", "Used"), ("operation", "RunInstances"))
    filters = json.dumps([{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in terms])
    out = aws("pricing", "get-products", "--service-code", "AmazonEC2", "--filters", filters,
              region="us-east-1", timeout=90) or {}
    prices = []
    for raw in out.get("PriceList") or []:
        item = json.loads(raw) if isinstance(raw, str) else raw
        for term in ((item.get("terms") or {}).get("OnDemand") or {}).values():
            for dim in (term.get("priceDimensions") or {}).values():
                usd = float((dim.get("pricePerUnit") or {}).get("USD") or 0)
                if usd > 0:
                    prices.append(usd)
    if not prices:
        raise Failed(f"no list price for {itype} in {region}")
    return min(prices)


# ---------------------------------------------------------------- estimate

def estimate(c: Cluster, pricer: Pricer) -> None:
    """Right-size requests, then count the nodes each pool needs."""
    if c.failed:
        if c.target.skip:
            return
        for p in c.target.pools:
            row = PoolRow(p.name, p.instance_type, p.capacity, p.count)
            row.price, row.note = pricer.hourly(c.cloud, c.region, p.instance_type, p.capacity, p.os)
            c.pools.append(row)
        return
    pods_on = defaultdict(list)
    for p in c.pods:
        pods_on[p.node].append(p)
    groups = defaultdict(list)
    for n in c.nodes:
        groups[(n.pool, n.instance_type, n.capacity)].append(n)
    for (pool, itype, capacity), nodes in sorted(groups.items()):
        count = len(nodes)
        cpu_alloc, mem_alloc = min(n.cpu_alloc for n in nodes), min(n.mem_alloc for n in nodes)
        ds_cpu = ds_mem = cpu_rec = mem_rec = 0.0
        ds_pods = pods = 0
        for n in nodes:
            ds_pods = max(ds_pods, sum(1 for p in pods_on[n.name] if p.kind in OVERHEAD_KINDS))
            for p in pods_on[n.name]:
                if p.kind in OVERHEAD_KINDS:
                    cpu, mem = p.request()
                    ds_cpu += cpu / count
                    ds_mem += mem / count
                else:
                    rcpu, rmem = p.request(recommended=True)
                    pods += 1
                    cpu_rec, mem_rec = cpu_rec + rcpu, mem_rec + rmem
        net_cpu, net_mem = cpu_alloc - ds_cpu, mem_alloc - ds_mem
        net_pods = min(n.pods_alloc for n in nodes) - ds_pods
        if net_cpu <= 0 or net_mem <= 0 or net_pods <= 0:
            needed = count
        else:
            needed = min(count, max(math.ceil(cpu_rec / (net_cpu * PACKING) - 1e-9),
                                    math.ceil(mem_rec / (net_mem * PACKING) - 1e-9),
                                    math.ceil(pods / (net_pods * PACKING) - 1e-9)))
        row = PoolRow(pool, itype, capacity, count, needed)
        row.price, row.note = pricer.hourly(c.cloud, c.region, itype, capacity, nodes[0].os)
        c.pools.append(row)
    keep_minimums(c)


def keep_minimums(c: Cluster) -> None:
    """Keep each pool at its autoscaler minimum, and at least 1 node, across its machine types."""
    minimums = {p.name: p.minimum or 0 for p in c.target.pools}
    for pool in sorted({r.pool for r in c.pools}):
        rows = [r for r in c.pools if r.pool == pool]
        short = max(1, minimums.get(pool, 0)) - sum(r.needed for r in rows)
        while short > 0 and any(r.needed < r.nodes for r in rows):
            max(rows, key=lambda r: r.nodes - r.needed).needed += 1
            short -= 1


# ---------------------------------------------------------------- pdf writer

MM, PX = 72 / 25.4, 0.75
PAGE_W, PAGE_H, SIDE = 210 * MM, 297 * MM, 16 * MM
WIDTH = PAGE_W - 2 * SIDE
GAP, CARD_GAP, SEC_GAP = 7 * MM, 4 * MM, 3 * MM
INK, BODY, MUTED, RULE, RULE2, PANEL = "#0F1222", "#3A3F55", "#6B7086", "#E4E7F2", "#EFF1F8", "#F6F7FB"
NAVY, DEEP, BONE, LILAC, HAZE, UNDERLINE = "#050B3D", "#070F52", "#FAF7F3", "#D9D6EE", "#D6DAF5", "#8E98EB"
BLUE, GREEN, GREEN_INK, MINT, SLATE, WHITE = "#3B5BF5", "#12A97A", "#117D60", "#E3F5EF", "#5B6396", "#FFFFFF"

# Adobe AFM advance widths of WinAnsi codes 32-255, in 1/1000 em
_HELVETICA = [int(w) for w in (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 556 556 556 556 556 556 556 556 556 556 "
    "278 278 584 584 584 556 1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 667 778 722 667 "
    "611 722 667 944 667 667 611 278 278 278 469 556 333 556 556 500 556 556 278 556 556 222 222 500 222 833 "
    "556 556 556 556 333 500 278 556 500 722 500 500 500 334 260 334 584 350 556 350 222 556 333 1000 556 556 "
    "333 1000 667 333 1000 350 611 350 350 222 222 333 333 350 556 1000 333 1000 500 333 944 350 500 667 278 "
    "333 556 556 556 556 260 556 333 737 370 556 584 333 737 333 400 584 333 333 333 556 537 278 333 333 365 "
    "556 834 834 834 611 667 667 667 667 667 667 1000 722 667 667 667 667 278 278 278 278 722 722 778 778 778 "
    "778 778 584 778 722 722 722 722 667 667 611 556 556 556 556 556 556 889 500 556 556 556 556 278 278 278 "
    "278 556 556 556 556 556 556 556 584 611 556 556 556 556 500 556 500").split()]
_HELVETICA_BOLD = [int(w) for w in (
    "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 556 556 556 556 556 556 556 556 556 556 "
    "333 333 584 584 584 611 975 722 722 722 722 667 611 778 722 278 556 722 611 833 722 778 667 778 722 667 "
    "611 722 667 944 667 667 611 333 278 333 584 556 333 556 611 556 611 556 333 611 611 278 278 556 278 889 "
    "611 611 611 611 389 556 333 611 556 778 556 556 500 389 280 389 584 350 556 350 278 556 500 1000 556 556 "
    "333 1000 667 333 1000 350 611 350 350 278 278 500 500 350 556 1000 333 1000 556 333 944 350 500 667 278 "
    "333 556 556 556 556 280 556 333 737 370 556 584 333 737 333 400 584 333 333 333 611 556 278 333 333 365 "
    "556 834 834 834 611 722 722 722 722 722 722 1000 722 667 667 667 667 278 278 278 278 722 722 778 778 778 "
    "778 778 584 778 722 722 722 722 667 667 611 556 556 556 556 556 556 889 556 556 556 556 556 278 278 278 "
    "278 611 611 611 611 611 611 611 584 611 611 611 611 611 556 611 556").split()]
# The bone logo as PDF paths, in its SVG's 1725 x 430 units, y up
_LOGO = (
    "180 380 m 205 404 l 229 380 l 205 355 l h 78 329 m 84 370 l 126 363 l 119 322 l h 30 235 m 15 266 l 46 282 "
    "l 62 251 l h 47 122 m 10 141 l 29 178 l 66 159 l h 122 48 m 87 43 l 82 77 l 116 83 l h 234 29 m 205 0 l "
    "175 29 l 205 59 l h 328 77 m 322 43 l 288 48 l 293 83 l h 380 178 m 399 141 l 362 122 l 343 159 l h 364 "
    "282 m 395 266 l 379 235 l 348 251 l h 284 363 m 325 370 l 332 329 l 290 322 l h 185 325 m 205 345 l 225 "
    "325 l 205 305 l h 84 222 m 71 248 l 96 261 l 109 235 l h 150 95 m 122 91 l 118 119 l 146 123 l h 292 119 m "
    "287 91 l 259 95 l 264 123 l h 313 261 m 338 248 l 326 222 l 300 235 l h 291 204 m 291 156 253 118 205 118 "
    "c 157 118 118 156 118 204 c 118 252 157 291 205 291 c 253 291 291 252 291 204 c h 718 316 m 718 277 l 609 "
    "135 l 720 135 l 714 92 l 547 92 l 547 131 l 656 274 l 556 274 l 556 316 l h 953 205 m 953 181 949 160 940 "
    "142 c 932 124 920 111 904 101 c 888 91 869 86 848 86 c 815 86 789 96 770 117 c 751 138 742 168 742 205 c "
    "742 228 746 249 755 266 c 763 284 776 298 792 308 c 807 318 826 323 848 323 c 881 323 907 312 925 291 c "
    "944 270 953 241 953 205 c h 799 205 m 799 178 803 158 811 146 c 819 133 832 127 848 127 c 864 127 876 133 "
    "884 146 c 892 159 896 178 896 205 c 896 231 892 250 884 263 c 876 275 864 282 848 282 c 816 282 799 256 "
    "799 205 c h 1199 205 m 1199 181 1194 160 1186 142 c 1177 124 1165 111 1149 101 c 1134 91 1115 86 1093 86 c "
    "1060 86 1034 96 1016 117 c 997 138 988 168 988 205 c 988 228 992 249 1000 266 c 1009 284 1021 298 1037 308 "
    "c 1053 318 1072 323 1094 323 c 1127 323 1152 312 1171 291 c 1189 270 1199 241 1199 205 c h 1045 205 m 1045 "
    "178 1049 158 1057 146 c 1065 133 1077 127 1093 127 c 1109 127 1121 133 1129 146 c 1137 159 1141 178 1141 "
    "205 c 1141 231 1137 250 1129 263 c 1121 275 1109 282 1094 282 c 1061 282 1045 256 1045 205 c h 1380 319 m "
    "1371 267 l 1364 269 1358 269 1351 269 c 1338 269 1327 265 1320 255 c 1312 246 1306 231 1302 212 c 1302 92 "
    "l 1247 92 l 1247 316 l 1294 316 l 1299 272 l 1305 288 1313 301 1323 309 c 1333 318 1345 322 1358 322 c "
    "1366 322 1374 321 1380 319 c h 1466 316 m 1466 92 l 1412 92 l 1412 316 l h 1473 397 m 1473 388 1470 380 "
    "1464 374 c 1457 367 1449 364 1439 364 c 1429 364 1421 367 1415 374 c 1408 380 1405 388 1405 397 c 1405 406 "
    "1408 414 1415 420 c 1421 427 1429 430 1439 430 c 1449 430 1457 427 1464 420 c 1470 414 1473 406 1473 397 c "
    "h 1584 412 m 1584 92 l 1530 92 l 1530 406 l h 1718 316 m 1644 218 l 1725 92 l 1663 92 l 1586 215 l 1661 "
    "316 l h f")

_DRAWN = {"\u2192": ("\xae", 987, 900), "\u2212": ("-", 549, 584)}  # Symbol code and width, drawn width (1/1000 em)


def _win(s: str) -> str:
    """s in WinAnsi code points: the nearest letter or ? for a character it lacks; the arrow and minus stay."""
    out = []
    for ch in s:
        if " " <= ch <= "~" or ch in _DRAWN:
            out.append(ch)
            continue
        for c in (ch, unicodedata.normalize("NFKD", ch)[:1]):
            try:
                out.append(" " if c < " " or c == "\x7f" else c.encode("cp1252").decode("latin-1"))
                break
            except UnicodeEncodeError:
                pass
        else:
            out.append("?")
    return "".join(out)


def _advance(t: str, size: float, bold: bool, spacing: float) -> float:
    widths = _HELVETICA_BOLD if bold else _HELVETICA
    return sum(_DRAWN[c][2] if c in _DRAWN else widths[ord(c) - 32] for c in t) * size / 1000 + spacing * len(t)


def text_width(s: str, size: float, bold: bool = False, spacing: float = 0.0) -> float:
    return _advance(_win(s), size, bold, spacing)


def _metrics(size: float, lh: float = None) -> tuple:
    """Line pitch and baseline offset in points, as the design's browser lays a line out (lh None: normal)."""
    a, d = int(size / PX * 0.96875 + 0.5) * PX, int(size / PX * 0.2421875 + 0.5) * PX
    box = a + d if lh is None else int(size * lh / PX) * PX
    return box, (box - a - d) / 2 + a


def _f(v: float, digits: int = 2) -> str:
    return ("%.*f" % (digits, v)).rstrip("0").rstrip(".")


def _rgb(c: str) -> str:
    return " ".join(_f(int(c[i:i + 2], 16) / 255, 3) for i in (1, 3, 5))


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _path(x: float, y: float, w: float, h: float, r: float) -> str:
    """A rectangle path from its bottom-left in page coordinates (y up), its corners rounded by r."""
    r = max(0.0, min(r, w / 2, h / 2))
    if not r:
        return "%s %s %s %s re" % (_f(x), _f(y), _f(w), _f(h))
    k, X, Y = r * 0.4477, x + w, y + h
    pts = [(x + r, y), (X - r, y), (X - k, y, X, y + k, X, y + r), (X, Y - r), (X, Y - k, X - k, Y, X - r, Y),
           (x + r, Y), (x + k, Y, x, Y - k, x, Y - r), (x, y + r), (x, y + k, x + k, y, x + r, y)]
    return " ".join(" ".join(map(_f, p)) + (" m", " l", " c")[min(i, 1) + (len(p) > 2)] for i, p in enumerate(pts))


class Pdf:
    """A4 pages of drawing operators. x runs from the left edge and y down from the top edge, in points."""

    def __init__(self):
        self.pages = []

    def page(self) -> None:
        self.ops, self.annots = [], []
        self.pages.append((self.ops, self.annots))

    def text(self, x: float, y: float, s: str, size: float, color: str = INK, bold: bool = False, align: str = "l",
             spacing: float = 0.0) -> float:
        """One line with its baseline at y, aligned l, r or c on x. Returns its width."""
        t = _win(s)
        w = _advance(t, size, bold, spacing)
        x -= {"l": 0, "r": w, "c": w / 2}[align]
        ops, art, b = ["BT %s rg %s Tc" % (_rgb(color), _f(spacing))], [], PAGE_H - y
        for run in re.findall("[\u2192\u2212]|[^\u2192\u2212]+", t):
            if run in _DRAWN:
                code, sym, drawn = _DRAWN[run]
                ops.append("1 0 0 1 %s %s Tm 3 Tr %s Tz /F3 %s Tf (%s) Tj 0 Tr 100 Tz"
                           % (_f(x), _f(b), _f(100 * drawn / sym), _f(size), code))
                art.append(self._drawn(run, x, b, size, color, bold))
            else:
                ops.append("1 0 0 1 %s %s Tm /F%d %s Tf (%s) Tj"
                           % (_f(x), _f(b), 2 if bold else 1, _f(size), _esc(run)))
            x += _advance(run, size, bold, spacing)
        if t:
            self.ops.append(" ".join(ops) + " ET" + "".join(art))
        return w

    @staticmethod
    def _drawn(c: str, x: float, b: float, size: float, color: str, bold: bool) -> str:
        """Helvetica's own minus, or an arrow of the same weight across the middle of the figures."""
        if c == "\u2212":
            x0, y0, x1, y1 = (40, 197, 544, 309) if bold else (39, 216, 545, 289)
            return " %s rg %s %s %s %s re f" % (_rgb(color), _f(x + x0 * size / 1000), _f(b + y0 * size / 1000),
                                               _f((x1 - x0) * size / 1000), _f((y1 - y0) * size / 1000))
        t, y, head = (0.112 if bold else 0.073) * size, b + 0.34 * size, 0.29 * size
        tip = x + 0.82 * size - t * 0.7071
        return " q %s RG %s w 0 J 0 j %s %s m %s %s l S %s %s m %s %s l %s %s l S Q" % (
            _rgb(color), _f(t), _f(x + 0.08 * size), _f(y), _f(tip), _f(y), _f(tip - head), _f(y + head), _f(tip),
            _f(y), _f(tip - head), _f(y - head))

    def rect(self, x: float, y: float, w: float, h: float, fill: str = None, stroke: str = None, lw: float = PX,
             r: float = 0.0) -> None:
        """A rectangle from its top-left (x, y), filled and/or stroked on its edge, its corners rounded by r."""
        self.ops.append("q %s%s%s %s Q" % ("%s rg " % _rgb(fill) if fill else "",
                                            "%s RG %s w " % (_rgb(stroke), _f(lw)) if stroke else "",
                                            _path(x, PAGE_H - y - h, w, h, r), "B" if fill and stroke else "f" if fill
                                            else "S"))

    def dashed(self, x: float, y: float, w: float, h: float, r: float, color: str = GREEN) -> None:
        """A 1.5px dashed outline just inside the box, its dashes spread evenly and centred on each side."""
        lw = 1.125
        w, h, r = w - lw, h - lw, max(0.0, min(r - lw / 2, (w - lw) / 2, (h - lw) / 2))
        around = 2 * (w + h) - (8 - 2 * math.pi) * r
        period = around / max(8, 2 * round(around / (7.2 * lw)))
        on = period * 0.62
        self.ops.append("q %s RG %s w [%s %s] %s d %s S Q" % (
            _rgb(color), _f(lw), _f(on), _f(period - on), _f(period - (w / 2 - r - on / 2) % period),
            _path(x + lw / 2, PAGE_H - y - h - lw / 2, w, h, r)))

    def line(self, x1: float, y1: float, x2: float, y2: float, color: str = RULE, lw: float = PX) -> None:
        self.ops.append("q %s RG %s w %s %s m %s %s l S Q" % (_rgb(color), _f(lw), _f(x1), _f(PAGE_H - y1), _f(x2),
                                                              _f(PAGE_H - y2)))

    def link(self, x: float, y: float, w: float, h: float, uri: str) -> None:
        self.annots.append("<< /Type /Annot /Subtype /Link /Rect [%s %s %s %s] /Border [0 0 0] /A << /S /URI /URI (%s) "
                           ">> >>" % (_f(x), _f(PAGE_H - y - h), _f(x + w), _f(PAGE_H - y), _esc(uri)))

    def logo(self, x: float, y: float, w: float) -> None:
        """The bone Zoorik logo, w points wide, from its top-left (x, y)."""
        s = w / 1725
        self.ops.append("q %s rg %s 0 0 %s %s %s cm %s Q" % (_rgb(BONE), _f(s), _f(s), _f(x), _f(PAGE_H - y - 430 * s),
                                                             _LOGO))

    def save(self, title: str) -> bytes:
        """The document: catalog, page tree, info, the three standard fonts, then each page and its content."""
        objs = [b"<< /Type /Catalog /Pages 2 0 R /ViewerPreferences << /DisplayDocTitle true >> >>",
                b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
                    " ".join("%d 0 R" % (7 + 2 * i) for i in range(len(self.pages))).encode(), len(self.pages)),
                b"<< /Title <FEFF%s> /Producer (Zoorik) >>" % title.encode("utf-16-be").hex().upper().encode()]
        objs += [b"<< /Type /Font /Subtype /Type1 /BaseFont /%s >>" % f for f in (
            b"Helvetica /Encoding /WinAnsiEncoding", b"Helvetica-Bold /Encoding /WinAnsiEncoding", b"Symbol")]
        for ops, annots in self.pages:
            data = zlib.compress("\n".join(ops).encode("latin-1"))
            page = ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %s %s] /Contents %d 0 R /Resources << /Font << "
                    "/F1 4 0 R /F2 5 0 R /F3 6 0 R >> >>" % (_f(PAGE_W), _f(PAGE_H), len(objs) + 2))
            objs.append((page + (" /Annots [%s]" % " ".join(annots) if annots else "") + " >>").encode())
            objs.append(b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream" % (len(data), data))
        out, offsets = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"), []
        for i, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n%s" % (len(objs) + 1, b"".join(b"%010d 00000 n \n" % o
                                                                              for o in offsets))
        out += b"trailer\n<< /Size %d /Root 1 0 R /Info 3 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
        return bytes(out)


def _span(sp, bold: bool, color: str) -> tuple:
    if isinstance(sp, str):
        return sp, bold, color, None
    t, b, c, u = (tuple(sp) + (None, None, None))[:4]
    return t, bold if b is None else b, c or color, u


def wrap(spans, size: float, width: float, bold: bool = False, color: str = INK, spacing: float = 0.0) -> list:
    """Spans in lines no wider than width, broken at spaces and newlines, after a hyphen only in a word wider than
    the line, and inside a word only when the part alone is wider; a separator dot at either end of a line is left
    out. A span is a str or (text, bold, color, url). Returns [(width, [(x, text, bold, color, url)])]."""
    units, space, glue = [], 0, False
    for sp in [spans] if isinstance(spans, str) else spans:
        t, b, c, u = _span(sp, bold, color)
        for tok in re.findall(r"\n| +|[^ \n-]*-|[^ \n-]+", t):
            if tok == "\n":
                units.append(None)
                space, glue = 0, False
            elif tok[0] == " ":
                space, glue = space + len(tok), False
            else:
                if glue:
                    units[-1][1].append((tok, b, c, u))
                else:
                    units.append((space, [(tok, b, c, u)]))
                space, glue = 0, not tok.endswith("-")
    lines, line, x, sw = [], [], 0.0, text_width(" ", size, False, spacing)
    sized = [unit and [(p, text_width(p[0], size, p[1], spacing)) for p in unit[1]] for unit in units]
    for i, unit in enumerate(units):
        if unit is None:
            lines.append((x, line))
            line, x = [], 0.0
            continue
        pieces = sized[i]
        uw, gap = sum(w for _, w in pieces), unit[0] * sw if line else 0.0
        word, j = uw, i + 1
        while (unit[0] or not i or units[i - 1] is None) and j < len(units) and units[j] and not units[j][0]:
            word, j = word + sum(w for _, w in sized[j]), j + 1
        if line and x + gap + (word if word <= width else uw) > width + 0.01:
            lines.append((x, line))
            line, x, gap = [], 0.0, 0.0
        x += gap
        for (t, b, c, u), w in pieces:
            if uw <= width:
                line.append((x, t, b, c, u))
                x += w
                continue
            run = None
            for ch in t:
                cw = text_width(ch, size, b, spacing)
                if line and x + cw > width + 0.01:
                    lines.append((x, line))
                    line, x, run = [], 0.0, None
                if run is None:
                    run = len(line)
                    line.append((x, ch, b, c, u))
                else:
                    line[run] = line[run][:1] + (line[run][1] + ch,) + line[run][2:]
                x += cw
    lines.append((x, line))
    for k, (w, ln) in enumerate(lines):
        if len(ln) > 1 and ln[-1][1] == "\xb7":
            ln = ln[:-1]
            w = ln[-1][0] + text_width(ln[-1][1], size, ln[-1][2], spacing)
        if len(ln) > 1 and ln[0][1] == "\xb7":
            w, ln = w - ln[1][0], [(p[0] - ln[1][0],) + p[1:] for p in ln[1:]]
        lines[k] = (w, ln)
    return lines


def _draw(pdf: Pdf, lines: list, x: float, top: float, width: float, size: float, metrics: tuple, align: str = "l",
          spacing: float = 0.0) -> None:
    """Lines from wrap() in a box at x, its top at top, metrics = (pitch, baseline offset)."""
    y = top + metrics[1]
    for w, pieces in lines:
        x0 = x + {"l": 0, "r": width - w, "c": (width - w) / 2}[align]
        for px, t, b, c, u in pieces:
            pw = pdf.text(x0 + px, y, t, size, c, b, spacing=spacing)
            if u:
                pdf.line(x0 + px, y + size * 0.27 + 0.4, x0 + px + pw, y + size * 0.27 + 0.4, UNDERLINE)
                pdf.link(x0 + px, y - size, pw, size * 1.3, u)
        y += metrics[0]


class Text:
    """A paragraph of spans wrapped to the content width; lh is the line height as a multiple of size (None: normal)."""

    def __init__(self, spans, size: float, color: str = INK, bold: bool = False, lh: float = None,
                 spacing: float = 0.0):
        self.lines, self.size, self.spacing = wrap(spans, size, WIDTH, bold, color, spacing), size, spacing
        self.m = _metrics(size, lh)
        self.h = len(self.lines) * self.m[0]

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        _draw(pdf, self.lines, x, y, WIDTH, self.size, self.m, spacing=self.spacing)


def headline(spans) -> Text:
    return Text(spans, 27, INK, True, 1.12, -0.675)


def lede(spans) -> Text:
    return Text(spans, 10.5, BODY, False, 1.6)


def fine(spans) -> Text:
    return Text(spans, 7.125, MUTED, False, 1.5)


class Kpis:
    """A row of KPI cards from [(value, label, value colour)]; a newline in a label starts its second line."""

    def __init__(self, items: list):
        self.items, self.w = items, (WIDTH - 6 * (len(items) - 1)) / len(items)
        self.labels = [wrap(label, 7.875, self.w - 19.5, color=MUTED) for _, label, _ in items]
        self.h = 42 + max(len(lines) for lines in self.labels) * _metrics(7.875, 1.4)[0]

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        for i, ((value, _, color), lines) in enumerate(zip(self.items, self.labels)):
            cx = x + i * (self.w + 6)
            pdf.rect(cx + PX / 2, y + PX / 2, self.w - PX, self.h - PX, PANEL, RULE, r=8.625)
            pdf.text(cx + 9.75, y + 25.5, value, 16.5, color, True, spacing=-0.33)
            _draw(pdf, lines, cx + 9.75, y + 33, self.w, 7.875, _metrics(7.875, 1.4))


class Section:
    """A numbered section heading."""

    def __init__(self, n: int, title: str):
        self.n, self.title, self.h = n, title, 19.5

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        pdf.rect(x, y, 19.5, 19.5, BLUE, r=9.75)
        pdf.text(x + 9.75, y + 13.5, str(self.n), 9.75, WHITE, True, "c")
        pdf.text(x + 27, y + 15.375, self.title, 15.75, INK, True, spacing=-0.236)


class Legend:
    """One line of keys: (label, fill) with a 10.5 x 5.25 swatch, or (label, fill, w, h, dashed)."""

    def __init__(self, items: list):
        self.items, self.h = items, 8.25

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        for item in self.items:
            label, fill, w, h, dashed = tuple(item) + (10.5, 5.25, False)[len(item) - 2:]
            pdf.rect(x, y + 6.75 - h, w, h, fill, r=1.5)
            if dashed:
                pdf.dashed(x, y + 6.75 - h, w, h, 1.5)
            x += w + 3.75
            x += pdf.text(x, y + 6.75, label, 7.125, MUTED) + 10.5


def pair_bars(today: float, after: float, top: float):
    """Art: the today bar (slate) over the after bar (blue), on one scale where top fills 112.5pt."""
    def art(pdf, x, y, w):
        for i, (v, color) in enumerate(((today, SLATE), (after, BLUE))):
            if pdf and v > 0:
                pdf.rect(x, y + 1.5 + 6.75 * i, min(112.5, w) * v / top, 5.25, color, r=2.625)
        return 15.0
    return art


class Cell:
    """A table cell: text wrapped to its column, a muted sub-line under it, and art under both. bold and color default
    to the row's. art is a function (pdf, x, y, w) that draws from the cell's top-left within width w and returns its
    height; with pdf None it only returns the height."""

    def __init__(self, text: str = "", sub: str = "", bold: bool = None, color: str = None, art=None):
        self.text, self.sub, self.bold, self.color, self.art = text, sub, bold, color, art


class Card:
    """A bordered card holding one table. cols are [(fraction of the width, "l" or "r")], head the column titles (a
    newline breaks one), rows lists of Cell or str, total an optional bold last row under a rule. The card opens with
    a title and a muted aside, or with a name, a muted meta line and a green pill; legend is Legend items. A card
    taller than the space left splits between rows, and each part repeats the column titles."""

    def __init__(self, cols: list, head: list, rows: list, total: list = None, title: str = "", aside: str = "",
                 name: str = "", meta: str = "", pill: str = "", legend: list = None):
        self.pad = (7.5, 9.0, 3.0) if name else (9.0, 10.5, 9.0)
        self.inner = WIDTH - 2 * (PX + self.pad[1])
        edges = [self.inner * sum(f for f, _ in cols[:i]) for i in range(len(cols) + 1)]
        self.cols = [(x0, x1 - x0, a) for (_, a), x0, x1 in zip(cols, edges, edges[1:])]
        self.title, self.aside, self.name, self.meta, self.pill = title, aside, name, meta, pill
        self.legend, self.lines, self.first = Legend(legend) if legend else None, None, True
        self.top_h = 16.5 if title else 0.0
        if name:
            room = self.inner - (text_width(pill, 8.25, True) + 27 if pill else 0)
            if text_width(name, 10.5, True) + 6 + text_width(meta, 7.5) > room:
                self.lines = wrap(name, 10.5, room, True), wrap(meta, 7.5, room, color=MUTED)
            self.top_h = 3 + (max(16.5, 1.5 + 12.75 * len(self.lines[0]) + 9 * len(self.lines[1])) if self.lines
                              else 16.5)
        self.top_h += 16.75 if legend else 0
        self.head = [wrap(t, 7.125, w - 9, color=MUTED) for t, (_, w, _) in zip(head, self.cols)]
        self.head_h = 8.25 * max(len(lines) for lines in self.head) + 8.25
        self.rows = [self._row(r, False) for r in rows] + ([self._row(total, True)] if total else [])
        self._size()

    def _row(self, cells: list, total: bool) -> tuple:
        size, laid = 9.0 if total else 8.25, []
        for cell, (_, w, _) in zip(cells, self.cols):
            c = cell if isinstance(cell, Cell) else Cell(cell)
            main = wrap(c.text, size, w - 9, total if c.bold is None else c.bold, c.color or INK) if c.text else []
            sub = wrap(c.sub, 7.125, w - 9, color=MUTED) if c.sub else []
            art = c.art(None, 0, 0, w - 9) if c.art else 0.0
            laid.append((c, main, sub, len(main) * _metrics(size)[0] + 8.25 * len(sub) + art))
        return 12 + max(cell[3] for cell in laid), total, size, laid

    def _size(self) -> None:
        self.h = 2 * PX + self.pad[0] + self.pad[2] + self.top_h * self.first + self.head_h + sum(
            r[0] for r in self.rows) + PX * (len(self.rows) - 1)
        self.min_h = self.h - sum(r[0] + PX for r in self.rows[1:])

    def split(self, room: float):
        """The rows that fit in room and the rest, as two cards; None if not one row fits."""
        used, k = self.h - sum(r[0] + PX for r in self.rows) + PX, 0
        while k < len(self.rows) and used + self.rows[k][0] + PX * bool(k) <= room:
            used, k = used + self.rows[k][0] + PX * bool(k), k + 1
        if k == len(self.rows) - 1 and k > 1 and self.rows[-1][1]:
            k -= 1
        if not k or k == len(self.rows):
            return None
        return self._part(self.rows[:k], self.first), self._part(self.rows[k:], False)

    def _part(self, rows: list, first: bool):
        part = object.__new__(Card)
        part.__dict__.update(self.__dict__, rows=rows, first=first)
        part._size()
        return part

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        pdf.rect(x + PX / 2, y + PX / 2, WIDTH - PX, self.h - PX, WHITE, RULE, r=9 - PX / 2)
        x, y, right = x + PX + self.pad[1], y + PX + self.pad[0], x + WIDTH - PX - self.pad[1]
        if self.first:
            if self.title:
                pdf.text(x, y + 8.25, self.title, 8.25, INK, True)
                pdf.text(right, y + 8.25, self.aside, 8.25, MUTED, align="r")
            if self.name:
                if self.pill:
                    w = text_width(self.pill, 8.25, True) + 15
                    pdf.rect(right - w, y + 1.5, w, 15, MINT, r=7.5)
                    pdf.text(right - 7.5, y + 12, self.pill, 8.25, GREEN_INK, True, "r")
                if self.lines:
                    _draw(pdf, self.lines[0], x, y + 1.5, self.inner, 10.5, (12.75, 10.5))
                    _draw(pdf, self.lines[1], x, y + 1.5 + 12.75 * len(self.lines[0]), self.inner, 7.5, (9, 7.5))
                else:
                    pdf.text(x + pdf.text(x, y + 12, self.name, 10.5, INK, True) + 6, y + 12, self.meta, 7.5, MUTED)
            y += self.top_h - (16.75 if self.legend else 0)
            if self.legend:
                self.legend.draw(pdf, x, y)
                y += 16.75
        n = max(len(lines) for lines in self.head)
        for (cx, w, align), lines in zip(self.cols, self.head):
            _draw(pdf, lines, x + cx + 4.5, y + 3.75 + 8.25 * (n - len(lines)), w - 9, 7.125, (8.25, 6.75), align)
        y += self.head_h
        pdf.line(x, y - PX / 2, right, y - PX / 2, RULE)
        for i, (h, total, size, laid) in enumerate(self.rows):
            for (c, main, sub, ch), (cx, w, align) in zip(laid, self.cols):
                top = y + 6 + int((h - 12 - ch) / 2 / PX) * PX
                _draw(pdf, main, x + cx + 4.5, top, w - 9, size, _metrics(size), align)
                top += len(main) * _metrics(size)[0]
                _draw(pdf, sub, x + cx + 4.5, top, w - 9, 7.125, (8.25, 6.75), align)
                if c.art:
                    c.art(pdf, x + cx + 4.5, top + 8.25 * len(sub), w - 9)
            y += h + PX
            if i + 1 < len(self.rows):
                pdf.line(x, y - PX / 2, right, y - PX / 2, RULE if self.rows[i + 1][1] else RULE2)


class NextSteps:
    """The navy Next steps card: a title over spans; a link is (text, True, WHITE, url)."""

    def __init__(self, title: str, spans: list):
        self.title, self.lines = title, wrap(spans, 8.25, WIDTH - 27, color=HAZE)
        self.h = 36 + 15.75 * len(self.lines)

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        pdf.rect(x, y, WIDTH, self.h, DEEP, r=10.5)
        pdf.text(x + 13.5, y + 21, self.title, 10.5, WHITE, True)
        _draw(pdf, self.lines, x + 13.5, y + 25.5, WIDTH - 27, 8.25, (15.75, 11.75))


_BANNER = 25.5 * MM + 57.75  # page 1's navy banner: padding, logo, title and subtitle lines


class Report:
    """The flowing A4 document. Page 1 opens with the navy banner, later pages with the slim bar, and every page ends
    with the footer. add() blocks in order: gap is the space above a block, dropped at the top of a page, and keep
    holds a block on the page where the next one starts. A block has h and draw(pdf, x, y); a Card also splits."""

    def __init__(self, title: str, sub: str, date: str, footer: str):
        self.title, self.sub, self.date, self.footer, self.blocks = title, sub, date, footer, []

    def add(self, block, gap: float = 0.0, keep: bool = False) -> None:
        self.blocks.append((block, gap, keep))

    def _pages(self) -> list:
        top, bottom = 24 * MM, PAGE_H - 16 * MM
        items, pages, y, i = list(self.blocks), [[]], _BANNER + GAP, 0

        def splits(b, room: float) -> bool:
            """A card splits when it is taller than a page, or when a third of a page or more is left for it."""
            return isinstance(b, Card) and (b.h > bottom - top or room >= (bottom - top) / 3)

        def need(j: int, at: float) -> float:
            b, gap, keep = items[j]
            h = b.min_h if splits(b, bottom - at - gap) else b.h
            return gap + h + (need(j + 1, at + gap + h) if keep and j + 1 < len(items) else 0)

        while i < len(items):
            b, gap, keep = items[i]
            fresh = not pages[-1]
            g = 0 if fresh else gap
            if y + g + b.h + (need(i + 1, y + g + b.h) if keep and i + 1 < len(items) else 0) <= bottom or fresh and (
                    not isinstance(b, Card) or b.h <= bottom - top):
                pages[-1].append((b, y + g))
                y, i = y + g + b.h, i + 1
                continue
            part = b.split(bottom - y - g) if splits(b, bottom - y - g) else None
            if part:
                pages[-1].append((part[0], y + g))
                items[i] = (part[1], 0, keep)
            elif fresh:
                pages[-1].append((b, y))
                i += 1
            pages.append([])
            y = top
        return pages

    def pdf(self) -> bytes:
        pages, pdf = self._pages(), Pdf()
        for n, blocks in enumerate(pages, 1):
            pdf.page()
            if n == 1:
                pdf.rect(0, 0, PAGE_W, _BANNER, NAVY)
                pdf.logo(SIDE, 10 * MM, 96)
                pdf.text(SIDE, 88.11, self.title, 19.5, BONE, True, spacing=-0.39)
                pdf.text(SIDE, 105.11, self.sub, 8.25, LILAC)
            else:
                pdf.rect(0, 0, PAGE_W, 16 * MM, NAVY)
                pdf.logo(SIDE, 8 * MM - 9, 72)
                x = PAGE_W - SIDE - text_width(self.title + "\xb7" + self.date, 7.125, False, 0.1425) - 9
                for t in (self.title, "\xb7", self.date):
                    x += pdf.text(x, 25.5, t, 7.125, LILAC, spacing=0.1425) + 4.5
            for block, y in blocks:
                block.draw(pdf, SIDE, y)
            pdf.text(SIDE, PAGE_H - 14.7, self.footer, 6.375, MUTED)
            pdf.text(PAGE_W - SIDE, PAGE_H - 14.7, "%d / %d" % (n, len(pages)), 6.375, MUTED, align="r")
        return pdf.save("Zoorik " + self.title)


# ---------------------------------------------------------------- end of pdf writer


# ---------------------------------------------------------------- report

GREY = "#9AA0B8"
SQUARES = 60  # machines, 5 rows of squares: a card with a larger pool draws each of its pools as one bar
MINUS, TO, DOT = "\u2212", " \u2192 ", " \xb7 "
POOL_COLS = [(.25, "l"), (.25, "l"), (.09, "r"), (.11, "r"), (.11, "r"), (.19, "r")]
WORKLOAD_COLS = [(.2189, "l"), (.2194, "r"), (.1023, "r"), (.2194, "r"), (.1340, "r"), (.1060, "r")]
WORKLOAD_HEAD = ["Workload", "CPU request, cores\ntoday \u2192 recommended", "CPU peak",
                 "Memory request, GiB\ntoday \u2192 recommended", "Memory peak", "Share of\nthe saving"]


class Names:
    """Stable short hashes for names, salted per run."""

    def __init__(self, names: bool, clusters: bool):
        self.names, self.clusters, self.salt = names or clusters, clusters, os.urandom(16)

    def _hash(self, prefix: str, value: str) -> str:
        return prefix + hashlib.sha256(self.salt + value.encode("utf-8")).hexdigest()[:8]

    def cluster(self, v: str) -> str:
        return self._hash("cluster-", v) if self.clusters else v

    def namespace(self, v: str) -> str:
        return self._hash("ns-", v) if self.names else v

    def workload(self, ns: str, v: str) -> str:
        return self._hash("wl-", ns + "/" + v) if self.names else v


@dataclass
class Fig:
    """Machines today and after, and what they cost a month in whole dollars (None without a list price)."""
    a: int
    b: int
    today: Optional[int] = None
    after: Optional[int] = None

    @property
    def saving(self) -> Optional[int]:
        return None if self.today is None else self.today - self.after

    def pct(self, money: bool) -> int:
        part, whole = (self.saving, self.today) if money else (self.a - self.b, self.a)
        return half_up(100 * part / whole) if whole else 0

    def key(self) -> tuple:
        """Largest saving first, then most machines removed."""
        return self.today is None, -(self.saving or 0), self.b - self.a


@dataclass
class Load:
    """A workload's CPU cores and memory bytes: requested today, recommended, and peak use (None if not read)."""
    name: str
    where: str
    req: tuple
    rec: tuple
    peak: Optional[tuple]
    frees: bool = True
    share: Optional[int] = None
    weight: float = 0.0


def figure(rows: list) -> Fig:
    """Each pool's cost is rounded to whole dollars first, so every total is the sum of the figures shown."""
    priced = [r for r in rows if r.price is not None]
    f = Fig(sum(r.nodes for r in rows), sum(r.needed for r in rows))
    if priced:
        f.today = sum(half_up(r.monthly(r.nodes)) for r in priced)
        f.after = sum(half_up(r.monthly(r.needed)) for r in priced)
    return f


def half_up(v: float) -> int:
    return int(math.floor(v + 0.5))


def usd(v: int) -> str:
    return f"${v:,}"


def count(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def cores(v: float) -> str:
    """One decimal, or two below 1."""
    return f"{v:,.1f}" if v >= 0.95 or not v else f"{v:.2f}" if v >= 0.005 else "<0.01"


def gib(v: float) -> str:
    s = cores(v / GIB)
    return s[:-2] if s.endswith(".0") else s


def sentence(text: str) -> str:
    """A reason with its first letter capitalized, unless it opens with a command such as kubectl."""
    return text if re.match(r"(kubectl|kubelogin|az|aws) ", text) else text[:1].upper() + text[1:]


def platform(c: Cluster) -> str:
    return {"azure": "AKS", "aws": "EKS", "other": "Kubernetes"}.get(c.cloud, "")


def saving_cell(f: Fig, money: bool, sub: str, why: str) -> Cell:
    """A saving: green when there is one, else $0 or 0 (and sub, or that it needs every machine), or why it has no
    price."""
    if money and f.today is None:
        return Cell("Not priced", why)
    if money and f.saving or not money and f.a > f.b:
        return Cell(MINUS + (usd(f.saving) if money else count(f.a - f.b, "machine")), sub, True, GREEN_INK)
    return Cell("$0" if money else "0", sub or ("needs every machine" if f.a == f.b else ""))


def card(cols: list, head: list, rows: list, total: list = None, flex: int = 0, **kw) -> Card:
    """A Card whose figures each stay on one line: a column too narrow for its widest figure takes room from flex."""
    fr = [f for f, _ in cols]
    for row, size, bold in [(r, 8.25, False) for r in rows] + ([(total, 9.0, True)] if total else []):
        for i, cell in enumerate(row):
            c = cell if isinstance(cell, Cell) else Cell(cell)
            if c.text and re.fullmatch("[\u2212$0-9,. \u2192<]+", c.text):
                need = (text_width(c.text, size, bold if c.bold is None else c.bold) + 9.5) / (WIDTH - 22.5)
                if need > fr[i]:
                    fr[flex], fr[i] = fr[flex] - need + fr[i], need
    return Card([(f, a) for f, (_, a) in zip(fr, cols)], head, rows, total, **kw)


def squares(total: int, removed: int, color: str, bar: bool):
    """Art: one 6.75pt square per machine, wrapping at the column (at most 144pt), the last removed dashed; or with
    bar, one bar across the column, kept then removed."""
    def art(pdf, x, y, w):
        if bar:
            if pdf:
                kept = min(w, 144) * (total - removed) / total
                pdf.rect(x, y, kept, 6.75, color, r=2.25)
                if removed:
                    pdf.rect(x + kept, y, min(w, 144) - kept, 6.75, MINT, r=2.25)
                    pdf.dashed(x + kept, y, min(w, 144) - kept, 6.75, 2.25)
            return 6.75
        per = max(1, int((min(w, 144) + 2.25) // 9))
        for i in range(total if pdf else 0):
            sx, sy = x + i % per * 9, y + i // per * 9
            pdf.rect(sx, sy, 6.75, 6.75, MINT if i >= total - removed else color, r=2.25)
            if i >= total - removed:
                pdf.dashed(sx, sy, 6.75, 6.75, 2.25)
        return -(-total // per) * 9 - 2.25
    return art


def mini_bar(req: float, rec: float, peak: Optional[float], top: float):
    """Art: request today (grey track), recommended (blue) and the peak tick, on one 82.5pt scale where top fills it."""
    def art(pdf, x, y, w):
        if pdf and top:
            s = 82.5 / top
            pdf.rect(x, y + 3.75, req * s, 4.5, RULE, r=2.25)
            pdf.rect(x, y + 3.75, rec * s, 4.5, BLUE, r=2.25)
            if peak is not None:
                pdf.rect(x + peak * s - 0.75, y + 2.25, 1.5, 7.5, DEEP, r=0.75)
        return 9.75
    return art


def loads(c: Cluster, names: Names, many: bool) -> list:
    """The cluster's workloads as the report names them, with the cluster's name too when there are many."""
    out = []
    for w in c.workloads:
        req, rec = [p.request() for p in w.pods], [p.request(recommended=True) for p in w.pods]
        ctrs = [x for p in w.pods for x in p.running()]
        peak = None
        if ctrs and all(x.cpu_use is not None for x in ctrs):
            peak = sum(x.cpu_use for x in ctrs), sum(x.mem_use for x in ctrs)
        out.append(Load(names.workload(w.namespace, w.name), (names.cluster(c.target.name) + DOT if many else "")
                        + names.namespace(w.namespace), (sum(r[0] for r in req), sum(r[1] for r in req)),
                        (sum(r[0] for r in rec), sum(r[1] for r in rec)), peak, w.kind not in OVERHEAD_KINDS))
    return out


def split(total: int, rows: list) -> bool:
    """Give each workload its whole share of total: total x 1/2 (CPU it frees / CPU all free + memory it frees /
    memory all free), the largest remainders rounded up so the shares add up to total. DaemonSets and static pods,
    which the estimate keeps at today's requests, free none. False if none frees any."""
    freed = [(max(0.0, x.req[0] - x.rec[0]), max(0.0, x.req[1] - x.rec[1])) if x.frees else (0.0, 0.0) for x in rows]
    cpu, mem = sum(f[0] for f in freed), sum(f[1] for f in freed)
    for x, f in zip(rows, freed):
        x.weight = total * ((f[0] / cpu if cpu else 0) + (f[1] / mem if mem else 0)) / (2 if cpu and mem else 1)
        x.share = int(x.weight)
    if not (cpu or mem):
        return False
    for x in sorted(rows, key=lambda x: x.share - x.weight)[:total - sum(x.share for x in rows)]:
        x.share += 1
    return True


def workload_row(label, rows: list, scale: Optional[list], money: bool) -> list:
    """One workload, or the sum of several (its peak only when each one's was read); a bar under each request when
    scale gives the bars' full lengths."""
    req, rec = [sum(x.req[i] for x in rows) for i in (0, 1)], [sum(x.rec[i] for x in rows) for i in (0, 1)]
    peak = [sum(x.peak[i] for x in rows) if all(x.peak for x in rows) else None for i in (0, 1)]
    cells = [label]
    for i, show in ((0, cores), (1, gib)):
        art = mini_bar(req[i], rec[i], peak[i], scale[i]) if scale else None
        cells.append(Cell(show(req[i]) + TO + show(rec[i]), bold=bool(scale) or None, art=art))
        cells.append(Cell("not read", color=MUTED) if peak[i] is None else cores(peak[i] / (1, GIB)[i]))
    shares = [x.share for x in rows if x.share is not None]
    if money:
        total = sum(shares)
        cells.append(Cell(usd(total), bold=True, color=GREEN_INK if total else None) if shares
                     else Cell("not priced", color=MUTED))
    return cells


def report(clusters: list, names: Names, when: datetime) -> bytes:
    """The PDF: what Coral would save, by cluster and node pool, the workload sizes behind it, and what is left out."""
    date = f"{when.day} {when:%B %Y}"
    shown = sorted(((c, figure(c.pools)) for c in clusters if not c.failed and c.pools),
                   key=lambda cf: (cf[1].key(), names.cluster(cf[0].target.name)))
    left = [c for c in clusters if c.failed or not c.pools]
    money = any(f.today is not None for _, f in shown)
    scope = [c for c, _ in shown] or clusters
    kinds, found = {platform(c) for c in scope} - {""}, count(len(scope), "cluster")
    plat = " and ".join(k for k in ("AKS", "EKS") if k in kinds) if kinds and kinds <= {"AKS", "EKS"} else "Kubernetes"
    rep = Report("Kubernetes cost assessment", DOT.join([plat, found, date]), date,
                 DOT.join(["Zoorik cost assessment", found, date]))
    n, tot = len(shown), figure([r for c, _ in shown for r in c.pools])
    pct, them = tot.pct(money), "them" if n > 1 else "it"
    if not shown:
        rep.add(headline("No cluster could be assessed"))
    else:
        if money and tot.saving:
            rep.add(headline(["Reduce your machine bill by ", (f"{pct}%", True, GREEN_INK)]))
        elif tot.a > tot.b:
            rep.add(headline(["Reduce your machine count by ", (f"{tot.pct(False)}%", True, GREEN_INK)]))
        else:
            rep.add(headline("Your clusters need every machine they run" if n > 1 else
                             "Your cluster needs every machine it runs"))
        if money:
            rep.add(lede([f"{count(n, 'cluster')}, {count(tot.a, 'machine')}: ", (f"{usd(tot.today)} a month", True),
                          " today, ", (usd(tot.after), True), f" after Coral rebalances {them}."]), 3 * MM)
            unpriced = sum(r.nodes for c, _ in shown for r in c.pools if r.price is None)
            if unpriced:
                rep.add(Text(f"{count(unpriced, 'machine')} with no list price {'is' if unpriced == 1 else 'are'} "
                             "left out of the money.", 8.25, MUTED))
        else:
            rep.add(lede([f"{count(n, 'cluster')}: ", (count(tot.a, "machine"), True), " today, ", (str(tot.b), True),
                          f" after Coral rebalances {them}."]), 3 * MM)
            rep.add(Text("No list prices: " + "; ".join(sorted({r.note for c, _ in shown for r in c.pools})) + ".",
                         8.25, MUTED))
        by = Counter(platform(c) for c, _ in shown)
        kpis = [(f"{MINUS}{pct}%" if pct else "0%", ("machine bill,\n" if money else "machines,\n")
                 + ("all clusters" if n > 1 else "this cluster"), GREEN_INK),
                (f"{tot.a}{TO}{tot.b}", "machines for\nthe same work", INK),
                (str(n), ("clusters\n" if n > 1 else "cluster\n") + DOT.join(
                    k if n == 1 else f"{by[k]} {'other' if k == 'Kubernetes' else k}"
                    for k in ("AKS", "EKS", "Kubernetes") if by[k]), INK)]
        if money:
            kpis.insert(0, (usd(tot.saving), f"saved every month,\nabout {usd(tot.saving * 12)} a year", GREEN_INK))
        rep.add(Kpis(kpis), 6 * MM)
    if n > 1:
        rows, top = [], max(f.today or 0 for _, f in shown) or 1
        for c, f in shown:
            name = Cell(names.cluster(c.target.name), DOT.join(filter(None, (platform(c), c.region))), bold=True)
            why = "; ".join(sorted({r.note for r in c.pools if r.price is None}))
            if not money:
                rows.append([name, f"{f.a}{TO}{f.b}", saving_cell(f, False, f"{f.pct(False)}% of its machines", why)])
            elif f.today is None:
                rows.append([name, f"{f.a}{TO}{f.b}", "", "", "", saving_cell(f, True, "", why)])
            else:
                rows.append([name, f"{f.a}{TO}{f.b}", Cell(art=pair_bars(f.today, f.after, top)), usd(f.today),
                             usd(f.after), saving_cell(f, True, f"{f.pct(True)}% of its bill", why)])
        label, machines = f"All {n} clusters", f"{tot.a}{TO}{tot.b}"
        if money:
            rep.add(card([(.1907, "l"), (.1219, "r"), (.3239, "l"), (.1082, "r"), (.1082, "r"), (.1471, "r")],
                         ["Cluster", "Machines", "Cost", "Today", "After", "Saving"], rows,
                         [label, machines, "", usd(tot.today), usd(tot.after),
                          Cell((MINUS if tot.saving else "") + usd(tot.saving), color=GREEN_INK)], 2,
                         title="Savings by cluster", aside="a month, at list prices",
                         legend=[("Today", SLATE), ("After Coral", BLUE)]), GAP)
        else:
            rep.add(card([(.4, "l"), (.3, "r"), (.3, "r")], ["Cluster", "Machines", "Saving"], rows,
                         [label, machines, Cell(MINUS + count(tot.a - tot.b, "machine") if tot.a > tot.b else "0",
                                                color=GREEN_INK)], title="Savings by cluster"), GAP)
    section, shares = 0, False
    if shown:
        section += 1
        rep.add(Section(section, "Savings by cluster and node pool"), GAP, keep=True)
        rep.add(Legend([("Machine kept", SLATE, 6.75, 6.75), ("Machine Coral would remove", MINT, 6.75, 6.75, True)]),
                SEC_GAP, keep=True)
        for i, (c, f) in enumerate(shown):
            rows, bar = [], max(r.nodes for r in c.pools) > SQUARES
            for r in sorted(c.pools, key=lambda r: (figure([r]).key(), r.pool, r.instance_type)):
                p = figure([r])
                sub = DOT.join(filter(None, (re.sub(r"^Standard_", "", r.instance_type).replace("_", " "),
                                             "Spot" if r.capacity == "spot" else "",
                                             "" if r.price is None else usd(half_up(r.monthly(1))) + " a machine")))
                row = [Cell(r.pool, sub, bold=True),
                       Cell(art=squares(r.nodes, r.nodes - r.needed, SLATE if r.needed < r.nodes else GREY, bar)),
                       f"{r.nodes}{TO}{r.needed}"]
                if money:
                    row += ["", ""] if p.today is None else [usd(p.today), usd(p.after)]
                rows.append(row + [saving_cell(p, money, "", r.note)])
            pill = ""
            if money and f.today is not None:
                pill = f"{MINUS}{usd(f.saving)} a month{DOT}{f.pct(True)}%" if f.saving else "$0 a month"
            elif f.a > f.b:
                pill = f"{MINUS}{count(f.a - f.b, 'machine')}{DOT}{f.pct(False)}%"
            meta = DOT.join(filter(None, (platform(c), c.region, f"{f.a}{TO}{f.b} machine{'s' if f.a > 1 else ''}")))
            rep.add(card(POOL_COLS if money else POOL_COLS[:3] + [(.41, "r")],
                         ["Node pool", "Machines", "", "Today", "After", "Saving"] if money else
                         ["Node pool", "Machines", "", "Saving"], rows, None, 1, name=names.cluster(c.target.name),
                         meta=meta, pill=pill), CARD_GAP if i else SEC_GAP)
        section += 1
        rep.add(Section(section, "Recommended sizes for your workloads"), GAP, keep=True)
        rows, notes = [], []
        for c, f in shown:
            basis = f.saving if money else f.a - f.b
            mine = loads(c, names, n > 1) if c.samples else []
            if not c.samples or basis and not split(basis, mine):
                why = f"usage not read ({c.metrics_note or 'no data'})" if not c.samples else \
                    "its workloads are already right-sized"
                if money and basis:
                    why += f"; its {usd(basis)} a month is not split by workload"
                notes.append(f"{names.cluster(c.target.name)}: {why}.")
            elif basis == 0:
                split(0, mine)
            rows += mine
        if rows:
            rows.sort(key=lambda x: (x.share is None, -x.weight, not x.frees, x.rec[0] - x.req[0], x.where, x.name))
            top, rest, shares = rows[:6], rows[6:], money
            scale = [max(max(x.req[i], x.rec[i], (x.peak or (0, 0))[i]) for x in top) for i in (0, 1)]
            body = [workload_row(Cell(x.name, x.where, bold=True), [x], scale, money) for x in top]
            if rest:
                body.append(workload_row(Cell(count(len(rest), "other workload"), color=MUTED), rest, None, money))
            rep.add(card(WORKLOAD_COLS if money else [(.3249, "l")] + WORKLOAD_COLS[1:5],
                         WORKLOAD_HEAD[:6 if money else 5], body,
                         workload_row(f"All {count(len(rows), 'workload')}", rows, None, money) if len(rows) > 1
                         else None, legend=[("Reserved today", RULE), ("Recommended", BLUE),
                                            ("Peak use", DEEP, 2.25, 5.25)]), SEC_GAP)
            if notes:
                rep.add(Text("\n".join(notes), 8.25, MUTED), SEC_GAP)
        else:
            reasons = {c.metrics_note or "no data" for c, _ in shown}
            rep.add(Text("Usage not read: " + (reasons.pop() if len(reasons) == 1 else "; ".join(
                f"{names.cluster(c.target.name)}, {c.metrics_note or 'no data'}" for c, _ in shown)) + ".", 8.25,
                MUTED), SEC_GAP)
    if left:
        section += 1
        rep.add(Section(section, "Not in the saving yet"), GAP, keep=True)
        rows = []
        for c in left:
            unpriced = sorted({r.note for r in c.pools if r.price is None})
            today = None if unpriced or not c.pools else sum(half_up(r.monthly(r.nodes)) for r in c.pools)
            machines, name = sum(r.nodes for r in c.pools), names.cluster(c.target.name)
            rows.append((today is None, -(today or 0), name, unpriced, [
                Cell(name, DOT.join(filter(None, (platform(c), c.region, count(machines, "machine").replace(
                    " ", "\xa0") if machines else ""))), bold=True), c.failed or "no nodes are running", today]))
        k = 3 if any(not r[0] or money and r[3] for r in rows) else 2
        body = []
        for _, _, _, unpriced, (cell, why, today) in sorted(rows, key=lambda r: r[:3]):
            if k == 2:
                body.append([cell, sentence(why)])
            else:
                body.append([cell, sentence("; ".join([why] + unpriced)), usd(today) if today is not None else
                             Cell("not priced", color=MUTED) if unpriced else ""])
        rep.add(card([(.2115, "l"), (.6883, "l"), (.1002, "r")] if k == 3 else [(.2115, "l"), (.7885, "l")],
                     ["Cluster", "Why", "Today"][:k], body, None, 1), SEC_GAP)
    rep.add(NextSteps("Next steps", [
        "1. Email this PDF to ", (SEND_TO, True, WHITE, "mailto:" + SEND_TO), ".\u00a0\u00a02. Book 30 minutes: ",
        ("cal.com/zoorik/talk", True, WHITE, "https://cal.com/zoorik/talk"), ".\u00a0\u00a0 3. Connect your clusters "
        "with one zoorikctl command each, and see these numbers live in the Coral console."]), GAP, keep=True)
    lead = ("Machine cost only, at public list prices" if money else "Machine counts only") + (
        "; each workload's share follows the CPU and memory it frees" if shares else "")
    rep.add(fine((lead + ". An estimate, not a quote. " if shown else "")
                 + "The script read your clusters and sent nothing."), SEC_GAP)
    return rep.pdf()


# ---------------------------------------------------------------- main

def parse_args(argv: Optional[list]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="k8s.py", usage=f"{ONE_LINER} [options]",
                                description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--context", action="append", metavar="NAME", help="assess this kube context (repeatable)")
    p.add_argument("--all-contexts", action="store_true", help="assess every context in your kubeconfig")
    p.add_argument("--discover", action="append", choices=("azure", "aws"),
                   help="find clusters with az (AKS) or aws (EKS); repeatable")
    p.add_argument("--subscription", metavar="ID", help="Azure subscription to search (implies --discover azure)")
    p.add_argument("--region", action="append", metavar="NAME",
                   help="AWS region to search, repeatable (implies --discover aws; default: every enabled region)")
    p.add_argument("--sample-minutes", type=float, default=3, metavar="N",
                   help="minutes to sample usage (default 3; 0 takes one sample)")
    p.add_argument("--no-prices", action="store_true", help="skip price lookups")
    p.add_argument("--no-invoke", action="store_true",
                   help="never use az aks command invoke (AKS clusters kubectl can't read are then not assessed)")
    p.add_argument("--anonymize", action="store_true", help="replace namespace and workload names with hashes")
    p.add_argument("--anonymize-clusters", action="store_true", help="also replace cluster names (implies --anonymize)")
    p.add_argument("--out", metavar="PATH", help="output file or folder")
    p.add_argument("--version", action="version", version=f"Zoorik Coral: Kubernetes assessment {VERSION}")
    args = p.parse_args(argv)
    if not math.isfinite(args.sample_minutes) or args.sample_minutes < 0:
        p.error("--sample-minutes must be a number, 0 or more")
    return args


def output_path(out: Optional[str], cloud_shell: str) -> str:
    """Choose the report's path before any work, so a folder that can't be written is found early."""
    name = f"zoorik-coral-assessment-{datetime.now(timezone.utc):%Y%m%d-%H%M}.pdf"
    if out:
        if out.endswith(("/", os.sep)) or os.path.isdir(out):
            path = os.path.join(out, name)
        else:
            path = out if out.lower().endswith(".pdf") else out + ".pdf"
        folder = os.path.dirname(os.path.abspath(path))
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            raise Failed(f"cannot create {folder}: {e.strerror}") from None
        if not os.access(folder, os.W_OK):
            raise Failed(f"cannot write to {folder}")
        return os.path.abspath(path)
    home = os.path.expanduser("~")
    for folder in [home] if cloud_shell else [os.getcwd(), home]:
        if os.access(folder, os.W_OK):
            return os.path.join(folder, name)
    raise Failed("cannot write to the current folder or the home folder; choose one with --out PATH")


def save(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


def unexpected(e: Exception) -> str:
    """Name the error and the last line of this script it passed through, for the report to Zoorik."""
    here = unexpected.__code__.co_filename
    line = next((f.lineno for f in reversed(traceback.extract_tb(e.__traceback__)) if f.filename == here), 0)
    return f"unexpected error ({type(e).__name__}, line {line})"


def assess(args: argparse.Namespace) -> int:
    say(f"Zoorik Coral: Kubernetes assessment {VERSION}. It sends nothing to Zoorik; you send the report yourself.")
    shell = "azure" if in_azure_cloud_shell() else "aws" if in_aws_cloudshell() else ""
    try:
        path = output_path(args.out, shell)
    except Failed as e:
        say(f"Cannot write the report: {e}")
        return 1
    targets, search_failed, why = choose_targets(args, shell)
    if not targets:
        if shell == "azure":
            say(("No cluster to assess." if search_failed else "No AKS cluster found in this subscription.")
                + f" To search another subscription: {ONE_LINER} --subscription ID")
        elif shell == "aws":
            say("No cluster to assess." if search_failed else "No EKS cluster found in this AWS account.")
        else:
            say(f"No cluster found{f' ({why})' if why else ''}: run this in Azure Cloud Shell or AWS CloudShell, "
                "or point kubectl at a cluster (kubectl config use-context NAME).")
        return 1
    if shell and (len(targets) > 5 or sum(t.source == "aks" and t.private for t in targets) > 1
                  or args.sample_minutes > 10):
        say("This run can take a while. Cloud Shell closes a session after 20 minutes without a key press, "
            "so press Enter now and then until the report path appears.")
    clusters, seen = [], []
    for i, t in enumerate(targets, 1):
        say(f"[{i}/{len(targets)}] {t.name}{f' ({t.cloud}, {t.region})' if t.cloud else ''}: ", end="")
        try:
            c = collect(t, not args.no_invoke)
        except Exception as e:
            c = Cluster(target=t, cloud=t.cloud, region=t.region, failed=unexpected(e))
        ids = {n.provider for n in c.nodes if n.provider}
        same = next((name for name, other in seen if ids & other), None)
        if same:
            say(f"same cluster as {same}; counted once")
            continue
        seen.append((t.name, ids))
        clusters.append(c)
        say(f"not assessed, {c.failed}" if c.failed else f"{len(c.nodes)} nodes, {len(c.pods)} pods")
    sample_all(clusters, args.sample_minutes)
    if not args.no_prices:
        say("Looking up list prices ...")
    pricer = Pricer(not args.no_prices)
    for c in clusters:
        apply_usage(c)
        estimate(c, pricer)

    data = report(clusters, Names(args.anonymize, args.anonymize_clusters), datetime.now(timezone.utc))
    try:
        save(path, data)
    except OSError as e:
        # A folder can pass the early check and still refuse the file (Windows ACLs, macOS privacy settings).
        home = os.path.join(os.path.expanduser("~"), os.path.basename(path))
        written = False
        if not args.out and home != path:
            try:
                save(home, data)
                path, written = home, True
            except OSError:
                pass
        if not written:
            say(f"Could not write {path}: {e.strerror}")
            return 1

    shown = [c for c in clusters if not c.failed and c.pools]
    tot = figure([r for c in shown for r in c.pools])
    priced = sum(r.nodes for c in shown for r in c.pools if r.price is not None)
    cost = f"{usd(tot.today)} at list prices" if tot.today is not None else "not available"
    if tot.today is not None and priced < tot.a:
        cost += f", for {priced} of {tot.a} machines (the rest have no list price; see the report)"
    saved = f"{usd(tot.saving)} per month ({tot.pct(True)}%)" if tot.today else "not available"
    say()
    say(f"Clusters:          {len(shown)} assessed"
        + (f", {len(clusters) - len(shown)} not assessed" if len(clusters) > len(shown) else ""))
    say(f"Monthly cost:      {cost}")
    say(f"Estimated savings: {saved}")
    say()
    say(f"Report: {path}")
    if shell == "azure":
        say(f"Download it: Cloud Shell toolbar > Manage files > Download, then enter {path}")
    elif shell == "aws":
        say(f"Download it: Actions > Download file, then enter {path}")
        say("(Download needs the full CloudShell page; if Actions is missing, open CloudShell in a new tab. "
            "A CloudShell VPC environment can't download files.)")
    say(f"Please e-mail it to {SEND_TO}")
    return 0


def stop(signum, _frame) -> None:
    """Turn a hang-up or terminate into a normal exit, so the temporary kubeconfig is removed."""
    raise SystemExit(128 + signum)


def main(argv: Optional[list] = None) -> int:
    global WORKDIR
    args = parse_args(argv)
    sys.stdout.reconfigure(errors="replace")
    for name in ("SIGHUP", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    WORKDIR = tempfile.mkdtemp(prefix="zoorik-k8s-")
    try:
        return assess(args)
    except KeyboardInterrupt:
        say("\nStopped.")
        return 130
    except Exception as e:
        say(f"\nStopped by an {unexpected(e)}. Please e-mail this line to {SEND_TO}.")
        return 1
    finally:
        shutil.rmtree(WORKDIR, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
