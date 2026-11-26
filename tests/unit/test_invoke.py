"""The command-invoke read: one az aks command invoke per cluster, every kind and the window's peaks in one answer."""

import os
import tempfile
import unittest
from typing import List, Mapping, Optional, Sequence

from tests.replay.clusters import (
    AKS,
    FORBIDDEN,
    METRICS_MISSING,
    SAMPLES,
    SMALL,
    Cluster,
    Sample,
    TopFailure,
    container,
    direct_calls,
    invoke_call,
    invoke_logs,
    invoke_text,
    kept_snapshot,
    large_cluster,
    pod,
    top,
)
from tests.replay.invoke_script import missing_tools, run_script
from tests.unit.clis import CloudTestCase, WatchingRunner
from zoorik.cloud.azure import AzureCli
from zoorik.cloud.started import StartedCommands
from zoorik.errors import ClusterUnreadable, UnexpectedResponse
from zoorik.kubectl.api import Snapshot
from zoorik.kubectl.direct import DOCS, DirectKubeApi, Kubeconfig
from zoorik.kubectl.invoke import (
    INVOKE_NAMESPACE,
    INVOKE_READ_SECONDS,
    LISTED,
    TOO_LARGE,
    InvokeKubeApi,
    script,
    sections_of,
)
from zoorik.kubectl.objects import BUDGETS, SCALING_KINDS
from zoorik.kubectl.usage import DEFAULT_SAMPLE_MINUTES, SampleWindow, Usage, peaks, top_table, usage_of

# Azure gives back at most 512 KB of a command invoke's logs.
LOGS_LIMIT = 512 * 1024
DEFAULT_WINDOW = SampleWindow(DEFAULT_SAMPLE_MINUTES)
# One minute takes three samples, as many as the small cluster has.
SHORT_WINDOW = SampleWindow(1)
UNAVAILABLE = "Error from server (ServiceUnavailable): the server is currently unable to handle the request"
UNAVAILABLE_REASON = "from server (ServiceUnavailable): the server is currently unable to handle the request"
ACCESS_DENIED = f"access denied (it needs cluster-wide read access; {DOCS})"
FORBIDDEN_REASON = f"cannot list nodes: {ACCESS_DENIED}"
NO_METRICS = Usage({}, "metrics-server is not installed")


def with_usage(text: str, usage: str) -> str:
    """Give the sections of text with the usage section's body replaced by usage."""
    return text[: text.index("@@usage\n")] + f"@@usage\n{usage}\n"


def without_section(text: str, name: str) -> str:
    """Give the sections of text without the section name."""
    lines = text.splitlines()
    start = lines.index(f"@@{name}")
    return "\n".join(lines[:start] + lines[start + 2 :]) + "\n"


def expected(cluster: Cluster) -> Snapshot:
    """Give the snapshot of cluster that both read methods should give."""
    return kept_snapshot(
        cluster, usage_of([top_table(sample) for sample in cluster.samples if isinstance(sample, str)], None)
    )


class InvokeTestCase(CloudTestCase):
    """A test that reads a cluster through command invoke, on made-up answers."""

    def setUp(self) -> None:
        """Make the run's temporary folder, its clock, and a runner that has had no call."""
        super().setUp()
        self.runner = WatchingRunner([], self.temp_root, self.clock)

    def invoke(self, logs: str, window: SampleWindow = DEFAULT_WINDOW) -> Snapshot:
        """Read the cluster whose command invoke gives logs; the runner that answered stays in self.runner."""
        host, self.runner = self.host([invoke_call(window, logs)])
        return InvokeKubeApi(AzureCli(host, StartedCommands()), AKS).read(window)

    def direct(self, cluster: Cluster, window: SampleWindow) -> Snapshot:
        """Read cluster with kubectl directly, for the answer the invoke read should match."""
        host, _ = self.host(direct_calls(cluster))
        return DirectKubeApi(host, Kubeconfig(os.path.join(self.temp_root, "kube", "one.yaml"), False)).read(window)

    def unreadable(self, logs: str) -> str:
        """Read a cluster whose command invoke gives logs, and give why it is unreadable."""
        with self.assertRaises(ClusterUnreadable) as raised:
            self.invoke(logs)
        return str(raised.exception)


class InvokeReadTest(InvokeTestCase):
    """InvokeKubeApi.read, on answers worked out in Python as the script works them out."""

    def test_one_call_reads_every_kind_and_one_peak_table_for_any_window(self) -> None:
        """One command invoke gives every kind and one table of peaks, whatever the window's number of samples."""
        logs = invoke_logs(invoke_text(SMALL))
        for minutes in (0, 3, 15):
            with self.subTest(minutes=minutes):
                window = SampleWindow(minutes)
                self.assertEqual(self.invoke(logs, window), expected(SMALL))
                self.assertEqual(len(self.runner.runs), 1)
                self.assertIn(f'[ "$i" -ge {window.samples} ] && break', script(window))

    def test_the_timeout_is_the_window_plus_the_reads(self) -> None:
        """The call waits for the window plus 600 s for the reads: a 15-minute window gives 1,500 s."""
        self.assertEqual(INVOKE_READ_SECONDS, 600)
        logs = invoke_logs(invoke_text(SMALL))
        for minutes, timeout in ((15, 1500.0), (3, 780.0), (0, 600.0)):
            with self.subTest(minutes=minutes):
                self.invoke(logs, SampleWindow(minutes))
                self.assertEqual(self.runner.runs[0][1], timeout)

    def test_an_answer_over_512_kb_is_named_and_not_read_again(self) -> None:
        """Logs with no end mark, as Azure keeps at most 512 KB of them, are named, and nothing is read again."""
        logs = invoke_logs(invoke_text(SMALL))
        self.assertEqual(self.unreadable(logs[: logs.index("@@end")]), TOO_LARGE)
        self.assertEqual(len(self.runner.runs), 1)
        self.assertEqual(TOO_LARGE, "its inventory is larger than command invoke can return (512 KB)")

    def test_a_kind_that_fails_makes_the_cluster_unreadable(self) -> None:
        """A kind whose section is an error stops the read, named by its kind."""
        logs = invoke_logs(invoke_text(SMALL, {"jobs": UNAVAILABLE}))
        self.assertEqual(self.unreadable(logs), f"cannot list jobs.batch: {UNAVAILABLE_REASON}")

    def test_forbidden_is_named(self) -> None:
        """A kind the identity may not list names the access it needs."""
        logs = invoke_logs(invoke_text(SMALL, {"nodes": FORBIDDEN.replace("\n", " ")}))
        self.assertEqual(self.unreadable(logs), FORBIDDEN_REASON)

    def test_an_autoscaler_or_budget_list_that_fails_leaves_the_cluster_read(self) -> None:
        """A section of either kind step 5 needs that is an error gives its reason, and the cluster is read."""
        for kind in SCALING_KINDS:
            with self.subTest(kind=kind.name):
                read = self.invoke(invoke_logs(invoke_text(SMALL, {kind.name: FORBIDDEN.replace("\n", " ")})))
                self.assertEqual(read.scaling.reason, f"cannot list {kind.resource}: {ACCESS_DENIED}")
                self.assertEqual(len(read.pods), 8)
                web = {item.name: item.labels for item in read.pods}["web-6d4b75cb6d-abcde"]
                self.assertEqual("tier" in web, kind is not BUDGETS)

    def test_missing_metrics_give_no_usage_named(self) -> None:
        """When no sample answered, the kinds are read and the usage says why."""
        cluster = Cluster(SMALL.objects, [TopFailure(METRICS_MISSING)] * len(SAMPLES))
        read = self.invoke(invoke_logs(invoke_text(cluster)))
        self.assertEqual(read.usage, NO_METRICS)
        self.assertEqual(len(read.nodes), 2)

    def test_the_command_invoke_pod_is_left_out(self) -> None:
        """The pod command invoke runs in, and its use, are not the customer's, so neither is kept."""
        invoked = pod(INVOKE_NAMESPACE, "command-3f2a9c", "node-one", [container("command", {"cpu": "100m"})])
        objects = {**SMALL.objects, "pods": [*SMALL.objects["pods"], invoked]}
        samples: List[Sample] = [f"{sample}{INVOKE_NAMESPACE} command-3f2a9c command 90m 120Mi\n" for sample in SAMPLES]
        read = self.invoke(invoke_logs(invoke_text(Cluster(objects, samples))))
        self.assertEqual(read, expected(SMALL))

    def test_logs_with_no_inventory_are_unexpected(self) -> None:
        """Logs with no begin mark hold no inventory."""
        with self.assertRaisesRegex(UnexpectedResponse, "^unexpected answer: command invoke gave no inventory$"):
            self.invoke("error: the pod was evicted\n")

    def test_an_inventory_that_does_not_decode_is_unexpected(self) -> None:
        """Text between the marks that is not gzip in base64 is refused."""
        for payload in ("bm90IGd6aXA=", "abc"):
            with self.subTest(payload=payload), self.assertRaisesRegex(UnexpectedResponse, "does not decode$"):
                self.invoke(f"@@begin\n{payload}\n@@end\n")

    def test_a_missing_section_is_unexpected(self) -> None:
        """An inventory without one of its sections is refused, naming the section."""
        logs = invoke_logs(without_section(invoke_text(SMALL), "jobs"))
        with self.assertRaisesRegex(UnexpectedResponse, "^unexpected answer: command invoke gave no jobs section$"):
            self.invoke(logs)

    def test_output_before_the_first_section_is_unexpected(self) -> None:
        """An inventory that starts with anything but a section is refused."""
        logs = invoke_logs("stray\n" + invoke_text(SMALL))
        with self.assertRaisesRegex(UnexpectedResponse, "output before its first section$"):
            self.invoke(logs)

    def test_a_section_that_is_not_a_list_is_unexpected(self) -> None:
        """A kind's section that is not a list of items is refused."""
        text = invoke_text(SMALL)
        lines = text.splitlines()
        lines[lines.index("@@nodes") + 1] = "{}"
        with self.assertRaisesRegex(
            UnexpectedResponse, "^unexpected answer: command invoke's nodes section is not a list$"
        ):
            self.invoke(invoke_logs("\n".join(lines) + "\n"))

    def test_an_unparsable_top_line_raises(self) -> None:
        """The script passes on any line that is not a container's use, and the read refuses it."""
        logs = invoke_logs(with_usage(invoke_text(SMALL), "shop  web-6d4b75cb6d-abcde  app  120m"))
        with self.assertRaises(UnexpectedResponse):
            self.invoke(logs)

    def test_a_cluster_sized_like_a_large_one_stays_under_the_limit(self) -> None:
        """200 nodes and 2,000 pods with 6,000 containers, at the default window, fit in one answer."""
        large = large_cluster()
        logs = invoke_logs(invoke_text(large))
        self.assertLess(len(logs.encode("ascii")), LOGS_LIMIT)
        read = self.invoke(logs)
        self.assertEqual(
            (len(read.nodes), len(read.pods), sum(len(item.containers) for item in read.pods)), (200, 2000, 6000)
        )
        self.assertEqual(len(read.usage.peaks), 6000)
        self.assertEqual(len(large.samples), DEFAULT_WINDOW.samples)


@unittest.skipIf(missing_tools(), "the command-invoke script runs on POSIX with sh, jq, awk, gzip and base64")
class ScriptTest(InvokeTestCase):
    """The script itself, run here against a made-up cluster, read by InvokeKubeApi."""

    def run_in_folder(
        self, cluster: Cluster, window: SampleWindow, failures: Optional[Mapping[str, str]] = None
    ) -> List[str]:
        """Run window's script against cluster, give the commands it ran, and keep its logs in self.logs."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        run = run_script(cluster, window, folder.name, failures)
        self.logs = run.logs
        return run.calls

    def test_the_script_reads_what_the_direct_read_reads(self) -> None:
        """On the same objects and samples, the script's answer reads as the same snapshot as the direct read."""
        calls = self.run_in_folder(SMALL, SHORT_WINDOW)
        read = self.invoke(self.logs, SHORT_WINDOW)
        self.assertEqual(read, self.direct(SMALL, SHORT_WINDOW))
        self.assertEqual(read, expected(SMALL))
        sample = "kubectl top pods -A --containers --no-headers --request-timeout=60s"
        self.assertEqual(
            calls,
            [f"kubectl get {kind.resource} -A -o json --chunk-size=500 --request-timeout=60s" for kind in LISTED]
            + [sample, "sleep 30", sample, "sleep 30", sample],
        )
        self.assertEqual([kind.name for kind in LISTED[:2]], ["horizontalpodautoscalers", "poddisruptionbudgets"])

    def test_a_label_no_placement_term_or_budget_uses_is_dropped_the_same_way(self) -> None:
        """The script's jq drops the labels the direct read drops, and keeps the same ones, a budget's key too."""
        self.run_in_folder(SMALL, SHORT_WINDOW)
        through_script = {item.name: item.labels for item in self.invoke(self.logs, SHORT_WINDOW).pods}
        direct = {item.name: item.labels for item in self.direct(SMALL, SHORT_WINDOW).pods}
        self.assertEqual(through_script, direct)
        self.assertEqual(
            through_script["web-6d4b75cb6d-abcde"],
            {"app": "web", "pod-template-hash": "6d4b75cb6d", "version": "v2", "tier": "front"},
        )

    def test_a_budget_list_that_fails_through_the_script_narrows_without_it(self) -> None:
        """Without the budgets, the script's jq narrows the labels as the direct read does, and keeps the reason."""
        self.run_in_folder(SMALL, SHORT_WINDOW, {BUDGETS.name: FORBIDDEN})
        read = self.invoke(self.logs, SHORT_WINDOW)
        self.assertEqual(read.scaling.reason, f"cannot list {BUDGETS.resource}: {ACCESS_DENIED}")
        self.assertEqual(
            {item.name: item.labels for item in read.pods}["web-6d4b75cb6d-abcde"],
            {"app": "web", "pod-template-hash": "6d4b75cb6d", "version": "v2"},
        )

    def test_the_script_prints_one_peak_table_for_any_window(self) -> None:
        """However many samples the window takes, the answer holds one row per container: its peak."""
        for minutes in (0, 15):
            with self.subTest(minutes=minutes):
                window = SampleWindow(minutes)
                samples: List[Sample] = [SAMPLES[number % len(SAMPLES)] for number in range(window.samples)]
                calls = self.run_in_folder(Cluster(SMALL.objects, samples), window)
                answered = [top_table(sample) for sample in samples if isinstance(sample, str)]
                self.assertEqual(len(sections_of(self.logs)["usage"].splitlines()), len(peaks(answered)))
                self.assertEqual(sum(1 for line in calls if line.startswith("kubectl top")), window.samples)
                self.assertEqual(self.invoke(self.logs, window).usage, usage_of(answered, None))

    def test_the_peak_is_the_same_for_both_readers_on_the_same_samples(self) -> None:
        """The awk in the cluster and usage.py here keep the same peaks, skipping the same failed sample."""
        samples: Sequence[Sample] = [SAMPLES[0], TopFailure("error: context deadline exceeded\n"), SAMPLES[2]]
        cluster = Cluster(SMALL.objects, samples)
        self.run_in_folder(cluster, SHORT_WINDOW)
        self.assertEqual(self.invoke(self.logs, SHORT_WINDOW).usage, self.direct(cluster, SHORT_WINDOW).usage)

    def test_failures_read_the_same_through_the_script(self) -> None:
        """Forbidden, missing metrics and an unparsable top line give what the direct read gives."""
        self.run_in_folder(SMALL, SHORT_WINDOW, {"nodes": FORBIDDEN})
        self.assertEqual(self.unreadable(self.logs), FORBIDDEN_REASON)
        no_metrics = Cluster(SMALL.objects, [TopFailure(METRICS_MISSING)] * len(SAMPLES))
        self.run_in_folder(no_metrics, SHORT_WINDOW)
        self.assertEqual(self.invoke(self.logs, SHORT_WINDOW).usage, NO_METRICS)
        self.assertEqual(self.direct(no_metrics, SHORT_WINDOW).usage, NO_METRICS)
        unparsable = Cluster(SMALL.objects, [SAMPLES[0], top("shop  web-6d4b75cb6d-abcde  app  120m"), SAMPLES[2]])
        self.run_in_folder(unparsable, SHORT_WINDOW)
        with self.assertRaises(UnexpectedResponse):
            self.invoke(self.logs, SHORT_WINDOW)

    def test_a_large_cluster_through_the_script_stays_under_the_limit(self) -> None:
        """The script's own answer for 200 nodes and 2,000 pods with 6,000 containers fits in 512 KB."""
        self.run_in_folder(large_cluster(), DEFAULT_WINDOW)
        self.assertLess(len(self.logs.encode("ascii")), LOGS_LIMIT)
        read = self.invoke(self.logs)
        self.assertEqual((len(read.nodes), len(read.pods), len(read.usage.peaks)), (200, 2000, 6000))
