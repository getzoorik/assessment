"""The direct read: kubectl with the run's temporary kubeconfig, every kind in pages, usage sampled over the window."""

import os
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlencode

from zoorik.cloud.cli import Host, Reason, reason_row, summary
from zoorik.cloud.fields import parse_json
from zoorik.errors import AssessmentError, ClusterUnreadable, ToolNotInstalled
from zoorik.kubectl.api import PAGE_SIZE, REQUEST_TIMEOUT, Snapshot, snapshot
from zoorik.kubectl.objects import KINDS, Json, Kind, slim_page
from zoorik.kubectl.usage import SAMPLE_INTERVAL, ContainerKey, Peak, SampleWindow, Usage, top_table, usage_of
from zoorik.system.process import Completed

DOCS = "see github.com/getzoorik/assessment#access-it-needs"
UNREACHABLE = "API server not reachable from here"
KUBELOGIN_MISSING = "kubelogin is not installed; install it with az aks install-cli"
# kubectl's error output, by pattern, as the reason the reader sees; the first row that matches wins.
KUBECTL_REASONS = (
    Reason(
        r"failed to connect to msi", ClusterUnreadable, "Cloud Shell sign-in issue; run az login, then run this again"
    ),
    Reason(r"executable kubelogin not found", ToolNotInstalled, KUBELOGIN_MISSING),
    Reason(r"aadsts", ClusterUnreadable, "Entra ID sign-in failed; run az login, then run this again"),
    Reason(r"metrics api not available", ClusterUnreadable, "metrics-server is not installed"),
    Reason(
        r"forbidden|authorizationfailed|accessdenied|unauthorizedoperation",
        ClusterUnreadable,
        f"access denied (it needs cluster-wide read access; {DOCS})",
    ),
    Reason(r"metrics.k8s.io", ClusterUnreadable, "metrics-server is not installed"),
    Reason(
        r"unauthorized|must be logged in", ClusterUnreadable, f"this identity has no access to the cluster ({DOCS})"
    ),
    Reason(r"az login", ClusterUnreadable, "not signed in to Azure; run az login"),
    Reason(
        r"unable to locate credentials", ClusterUnreadable, "no AWS credentials; run aws configure or aws sso login"
    ),
    Reason(r"expiredtoken", ClusterUnreadable, "AWS credentials have expired"),
    Reason(
        r"i/o timeout|dial tcp|no such host|connection refused|was refused|no route to host|network is unreachable"
        r"|unable to connect|deadline exceeded|timeout exceeded|tls handshake timeout|timed out",
        ClusterUnreadable,
        UNREACHABLE,
    ),
)
# A reachable API server answers /version at once, so its check waits less than the lists do.
VERSION_REQUEST_TIMEOUT = "20s"
VERSION_TIMEOUT = 60.0
LIST_TIMEOUT = 120.0
TOP_TIMEOUT = 180.0
CONVERT_TIMEOUT = 60.0
TOP = ("top", "pods", "-A", "--containers", "--no-headers")


def failure(text: str) -> AssessmentError:
    """Give the typed failure for kubectl's error text: its row of KUBECTL_REASONS, else a short reason."""
    row = reason_row(KUBECTL_REASONS, text)
    return ClusterUnreadable(summary(text)) if row is None else row.kind(row.text)


def listing_failure(kind: Kind, text: str) -> AssessmentError:
    """Give the typed failure for a kind that could not be listed, naming the kind."""
    error = failure(text)
    return type(error)(f"cannot list {kind.resource}: {error}")


@dataclass(frozen=True)
class Kubeconfig:
    """The run's temporary kubeconfig for one cluster."""

    path: str
    # The cluster's users sign in through Entra ID, so kubectl needs kubelogin to sign in with the Azure CLI.
    entra_id: bool


class DirectKubeApi:
    """A cluster read with kubectl directly: /version first, then every kind in pages, then the window's samples."""

    def __init__(self, host: Host, kubeconfig: Kubeconfig) -> None:
        """Run kubectl on host's runner with kubeconfig, waiting on host's clock, its cache in the temporary folder."""
        self._runner = host.runner
        self._clock = host.clock
        self._kubeconfig = kubeconfig
        self._cache = os.path.join(host.temp_root, "cache")

    def read(self, window: SampleWindow) -> Snapshot:
        """Read every kind and sample usage over window; a kind that can't be read makes the cluster unreadable."""
        if self._kubeconfig.entra_id:
            self._convert()
        checked = self._kubectl(["get", "--raw", "/version"], VERSION_REQUEST_TIMEOUT, VERSION_TIMEOUT)
        if checked.exit != 0:
            raise failure(checked.stderr)
        items: Dict[str, List[Json]] = {kind.name: self._list(kind) for kind in KINDS}
        return snapshot(items, self._usage(window))

    def _convert(self) -> None:
        """Make the kubeconfig sign in through kubelogin with the Azure CLI's sign-in."""
        argv = ["kubelogin", "convert-kubeconfig", "-l", "azurecli", "--kubeconfig", self._kubeconfig.path]
        try:
            completed = self._runner.run(argv, CONVERT_TIMEOUT, {})
        except ToolNotInstalled:
            raise ToolNotInstalled(KUBELOGIN_MISSING) from None
        if completed.exit != 0:
            raise failure(completed.stderr)

    def _kubectl(self, args: Sequence[str], request_timeout: str, timeout: float) -> Completed:
        """Run kubectl with args against the cluster; its discovery cache stays in the run's temporary folder."""
        argv = ["kubectl", "--kubeconfig", self._kubeconfig.path, "--cache-dir", self._cache]
        return self._runner.run([*argv, "--request-timeout", request_timeout, *args], timeout, {})

    def _list(self, kind: Kind) -> List[Json]:
        """List every item of kind, page by page, keeping only what the allowlist keeps."""
        items: List[Json] = []
        token: Optional[str] = None
        while True:
            query: Mapping[str, object] = (
                {"limit": PAGE_SIZE} if token is None else {"limit": PAGE_SIZE, "continue": token}
            )
            completed = self._kubectl(
                ["get", "--raw", f"{kind.path}?{urlencode(query)}"], REQUEST_TIMEOUT, LIST_TIMEOUT
            )
            if completed.exit != 0:
                raise listing_failure(kind, completed.stderr)
            page = slim_page(parse_json(completed.stdout, "kubectl"), kind)
            items.extend(page.items)
            if page.token is None:
                return items
            token = page.token

    def _usage(self, window: SampleWindow) -> Usage:
        """Take the window's samples, SAMPLE_INTERVAL seconds apart from its start; a sample that fails is skipped."""
        tables: List[Dict[ContainerKey, Peak]] = []
        last_failure: Optional[str] = None
        start = self._clock.monotonic()
        for index in range(window.samples):
            self._clock.sleep(max(0.0, start + index * SAMPLE_INTERVAL - self._clock.monotonic()))
            completed = self._kubectl(TOP, REQUEST_TIMEOUT, TOP_TIMEOUT)
            if completed.exit == 0:
                tables.append(top_table(completed.stdout))
            else:
                last_failure = str(failure(completed.stderr))
        return usage_of(tables, last_failure)
