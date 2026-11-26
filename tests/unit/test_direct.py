"""The direct read: kubectl with the run's kubeconfig, every kind in pages, then usage sampled over the window."""

import dataclasses
import json
import os
from typing import List, Mapping, Sequence

from tests.replay.clusters import (
    FORBIDDEN,
    KUBECONFIG,
    METRICS_MISSING,
    SAMPLES,
    SMALL,
    Cluster,
    Sample,
    TopFailure,
    direct_calls,
    kept,
    kubectl,
    list_calls,
    top,
    version_call,
)
from tests.replay.fake_cloud import VirtualClock
from tests.replay.recording import Call, Failure, call_key
from tests.replay.scenarios import call
from tests.unit.clis import CloudTestCase, WatchingRunner
from zoorik.cloud.cli import Host
from zoorik.errors import ClusterUnreadable, ToolNotInstalled, UnexpectedResponse
from zoorik.kubectl.api import Snapshot, snapshot
from zoorik.kubectl.direct import (
    DOCS,
    KUBECTL_REASONS,
    KUBELOGIN_MISSING,
    UNREACHABLE,
    DirectKubeApi,
    Kubeconfig,
    failure,
)
from zoorik.kubectl.objects import KINDS, NODES, PODS, REPLICASETS
from zoorik.kubectl.usage import SampleWindow, Usage, top_table, usage_of
from zoorik.system.process import Completed

# One minute: a sample at the start, one at 30 s and one at 60 s, as many as the small cluster has.
WINDOW = SampleWindow(1)
UNAVAILABLE = "Error from server (ServiceUnavailable): the server is currently unable to handle the request\n"
UNAVAILABLE_REASON = "from server (ServiceUnavailable): the server is currently unable to handle the request"
METRICS_NOT_FOUND = (
    "Error from server (NotFound): the server could not find the requested resource (get pods.metrics.k8s.io)\n"
)
CONVERT = ["kubelogin", "convert-kubeconfig", "-l", "azurecli", "--kubeconfig", KUBECONFIG]
VERSION = kubectl("get", "--raw", "/version", request_timeout="20s")


class TimedRunner(WatchingRunner):
    """A watching runner that also keeps the clock's time when each call starts."""

    def __init__(self, calls: Sequence[Call], temp_root: str, clock: VirtualClock) -> None:
        """Answer from calls, timing each on clock."""
        super().__init__(calls, temp_root, clock)
        self._clock = clock
        self.times: List[float] = []

    def run(self, argv: Sequence[str], timeout: float, overrides: Mapping[str, str]) -> Completed:
        """Keep the time, then the call."""
        self.times.append(self._clock.monotonic())
        return super().run(argv, timeout, overrides)


class DirectReadTest(CloudTestCase):
    """DirectKubeApi.read."""

    def setUp(self) -> None:
        """Make the run's temporary folder, its clock, and an empty list of calls made."""
        super().setUp()
        self.runner = TimedRunner([], self.temp_root, self.clock)

    def read(self, calls: Sequence[Call], *, entra_id: bool = False) -> Snapshot:
        """Read the cluster whose kubectl gives calls; the runner that answered stays in self.runner."""
        self.runner = TimedRunner(calls, self.temp_root, self.clock)
        kube = Kubeconfig(os.path.join(self.temp_root, "kube", "one.yaml"), entra_id)
        return DirectKubeApi(Host(self.runner, self.clock, self.temp_root, False), kube).read(WINDOW)

    def keys(self) -> List[str]:
        """Give the key of each call the runner was given, in order."""
        return [call_key(argv, self.temp_root) for argv in self.runner.argvs]

    def unreadable(self, calls: Sequence[Call], *, entra_id: bool = False) -> str:
        """Read a cluster whose read fails, and give the failure's reason."""
        with self.assertRaises(ClusterUnreadable) as raised:
            self.read(calls, entra_id=entra_id)
        return str(raised.exception)

    def test_every_kind_is_listed_in_pages_of_500(self) -> None:
        """/version comes first, then each of the eight kinds page by page, then the samples."""
        calls = direct_calls(SMALL, {kind.name: 1 for kind in KINDS})
        self.read(calls)
        self.assertEqual(self.keys(), [made.key for made in calls])
        lists = [made.argv[-1] for made in calls[1 : -len(SAMPLES)]]
        self.assertEqual(sorted({path.split("?")[0] for path in lists}), sorted(kind.path for kind in KINDS))
        self.assertIn("/api/v1/pods?limit=500&continue=page3%3D", lists)
        self.assertEqual(
            [timeout for _, timeout, _ in self.runner.runs],
            [60.0] + [120.0] * len(lists) + [180.0] * len(SAMPLES),
        )
        self.assertEqual([overrides for _, _, overrides in self.runner.runs], [{}] * len(calls))

    def test_the_snapshot_holds_what_the_allowlist_keeps(self) -> None:
        """The snapshot holds every kind's kept items, read, and each container's peak over the samples."""
        items = {kind.name: kept(kind, SMALL.objects[kind.name]) for kind in KINDS}
        usage = usage_of([top_table(sample) for sample in SAMPLES], None)
        self.assertEqual(self.read(direct_calls(SMALL)), snapshot(items, usage))

    def test_samples_start_30_seconds_apart_however_long_each_takes(self) -> None:
        """Each sample starts 30 s after the one before, counted from the first, even when each takes 5 s."""
        calls = [dataclasses.replace(made, seconds=5.0) if "top" in made.argv else made for made in direct_calls(SMALL)]
        self.read(calls)
        self.assertEqual(self.runner.times[-len(SAMPLES) :], [0.0, 30.0, 60.0])
        self.assertEqual(self.clock.monotonic(), 65.0)

    def test_a_kubelogin_conversion_comes_first(self) -> None:
        """A cluster whose users sign in through Entra ID gets its kubeconfig converted to the Azure CLI's sign-in."""
        calls = [call(CONVERT), *direct_calls(SMALL)]
        self.read(calls, entra_id=True)
        self.assertEqual(self.keys(), [made.key for made in calls])
        self.assertEqual(self.runner.runs[0][1], 60.0)

    def test_kubelogin_missing_is_named(self) -> None:
        """With no kubelogin, the read stops and says how to install it, whether the run or kubectl found it missing."""
        missing = Call(json.dumps(CONVERT), CONVERT, Failure("ToolNotInstalled", "kubelogin is not installed"), {}, 0.0)
        with self.assertRaisesRegex(ToolNotInstalled, f"^{KUBELOGIN_MISSING}$"):
            self.read([missing], entra_id=True)
        stderr = (
            "error: getting credentials: exec: executable kubelogin not found\n\nIt looks like you are trying to use"
        )
        with self.assertRaisesRegex(ToolNotInstalled, f"^{KUBELOGIN_MISSING}$"):
            self.read([call(VERSION, exit_code=1, stderr=stderr)])

    def test_a_failed_conversion_is_named(self) -> None:
        """A conversion that fails gives its reason, and nothing else runs."""
        converted = call(CONVERT, exit_code=1, stderr="AADSTS50076: due to a configuration change you must sign in\n")
        reason = self.unreadable([converted], entra_id=True)
        self.assertEqual(reason, "Entra ID sign-in failed; run az login, then run this again")
        self.assertEqual(len(self.runner.runs), 1)

    def test_an_api_server_that_cannot_be_reached_is_named(self) -> None:
        """When /version can't be reached, the read stops there and says so."""
        error = "Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout\n"
        self.assertEqual(self.unreadable([call(VERSION, exit_code=1, stderr=error)]), UNREACHABLE)
        self.assertEqual(len(self.runner.runs), 1)

    def test_forbidden_is_named(self) -> None:
        """A list the identity may not read names the access it needs."""
        nodes = call(kubectl("get", "--raw", "/api/v1/nodes?limit=500"), exit_code=1, stderr=FORBIDDEN)
        self.assertEqual(
            self.unreadable([version_call(), nodes]),
            f"cannot list nodes: access denied (it needs cluster-wide read access; {DOCS})",
        )

    def test_a_kind_that_fails_makes_the_cluster_unreadable(self) -> None:
        """A kind that fails stops the read with no snapshot, and no sample is taken."""
        before = [list_calls(kind, SMALL.objects[kind.name]) for kind in (NODES, PODS, REPLICASETS)]
        jobs = call(kubectl("get", "--raw", "/apis/batch/v1/jobs?limit=500"), exit_code=1, stderr=UNAVAILABLE)
        calls = [version_call(), *(page for pages in before for page in pages), jobs, *direct_calls(SMALL)[-3:]]
        self.assertEqual(self.unreadable(calls), f"cannot list jobs.batch: {UNAVAILABLE_REASON}")
        self.assertEqual(len(self.runner.runs), len(calls) - len(SAMPLES))

    def test_a_page_that_fails_makes_the_cluster_unreadable(self) -> None:
        """A later page that fails stops the read as the first page would."""
        first = list_calls(PODS, SMALL.objects["pods"], per_page=5)[0]
        second = call(
            kubectl("get", "--raw", "/api/v1/pods?limit=500&continue=page1%3D"), exit_code=1, stderr=UNAVAILABLE
        )
        calls = [version_call(), *list_calls(NODES, SMALL.objects["nodes"]), first, second]
        self.assertEqual(self.unreadable(calls), f"cannot list pods: {UNAVAILABLE_REASON}")
        self.assertEqual(len(self.runner.runs), len(calls))

    def test_an_answer_of_another_kind_is_unexpected(self) -> None:
        """A list that answers with another kind of object is refused."""
        status = json.dumps({"kind": "Status", "apiVersion": "v1", "status": "Failure"})
        calls = [version_call(), call(kubectl("get", "--raw", "/api/v1/nodes?limit=500"), status)]
        with self.assertRaisesRegex(UnexpectedResponse, "^unexpected answer: kubectl gave a Status for nodes$"):
            self.read(calls)

    def test_missing_metrics_give_no_usage_named(self) -> None:
        """When no sample answers because metrics-server is missing, the kinds are read and the usage says why."""
        for stderr in (METRICS_MISSING, METRICS_NOT_FOUND):
            with self.subTest(stderr=stderr):
                read = self.read(direct_calls(Cluster(SMALL.objects, [TopFailure(stderr)] * len(SAMPLES))))
                self.assertEqual(read.usage, Usage({}, "metrics-server is not installed"))
                self.assertEqual(len(read.nodes), 2)

    def test_a_failed_sample_is_skipped(self) -> None:
        """A sample that fails gives nothing; the peaks come from the samples that answered."""
        samples: List[Sample] = [SAMPLES[0], TopFailure("error: context deadline exceeded\n"), SAMPLES[2]]
        read = self.read(direct_calls(Cluster(SMALL.objects, samples)))
        self.assertEqual(read.usage, usage_of([top_table(SAMPLES[0]), top_table(SAMPLES[2])], None))

    def test_an_unparsable_top_line_raises(self) -> None:
        """A top answer with a line that is not a container's use is refused."""
        samples = [SAMPLES[0], top("shop  web-6d4b75cb6d-abcde  app  120m"), SAMPLES[2]]
        with self.assertRaises(UnexpectedResponse):
            self.read(direct_calls(Cluster(SMALL.objects, samples)))


class FailureTest(CloudTestCase):
    """failure and KUBECTL_REASONS."""

    def test_kubectls_errors_are_named(self) -> None:
        """Each kind of kubectl error gets its reason; one that no row names keeps its first line, without names."""
        cases = [
            (
                "error: You must be logged in to the server (Unauthorized)",
                f"this identity has no access to the cluster ({DOCS})",
            ),
            ("Please run 'az login' to setup account.", "not signed in to Azure; run az login"),
            (
                "Unable to locate credentials. You can configure credentials by running",
                "no AWS credentials; run aws configure or aws sso login",
            ),
            (
                "An error occurred (ExpiredToken) when calling the GetCallerIdentity operation",
                "AWS credentials have expired",
            ),
            ("dial tcp: lookup one.example.test on 10.0.0.2:53: no such host", UNREACHABLE),
            ("failed to connect to MSI endpoint", "Cloud Shell sign-in issue; run az login, then run this again"),
            (
                'error: the server doesn\'t have a resource type "csinodes"',
                "the server doesn't have a resource type ...",
            ),
        ]
        for text, reason in cases:
            with self.subTest(text=text):
                error = failure(text)
                self.assertEqual((type(error), str(error)), (ClusterUnreadable, reason))

    def test_only_a_missing_kubelogin_is_a_missing_tool(self) -> None:
        """Every reason makes the cluster unreadable, except a missing kubelogin, which is a tool to install."""
        kinds = {row.text: row.kind for row in KUBECTL_REASONS}
        self.assertEqual(kinds.pop(KUBELOGIN_MISSING), ToolNotInstalled)
        self.assertEqual(set(kinds.values()), {ClusterUnreadable})
