"""Recording a run, saving it, and replaying it on another temporary folder."""

import collections
import gzip
import json
import os
import stat
import sys
import tempfile
import unittest
from typing import Deque, Dict, List, Mapping, Optional, Sequence

from tests.replay.clusters import SPREAD, container, pod
from tests.replay.fake_cloud import FakeRun
from tests.replay.recording import (
    REPO,
    Call,
    Failure,
    Recorder,
    RecordingRefused,
    call_key,
    load,
    refuse_inside,
    save,
)
from tests.replay.redact import Redactor
from tests.replay.replay import ReplayHttp, ReplayPrompt, ReplayRunner
from tests.replay.scenarios import FACTS
from zoorik.errors import NotSignedIn
from zoorik.kubectl.objects import Json
from zoorik.system.clock import Clock
from zoorik.system.process import Completed
from zoorik.system.runtime import Runtime

PRICES = "https://prices.example.test/api/retail/prices?$filter=one"
NODES = {"kind": "NodeList", "items": [{"metadata": {"name": "node-one", "annotations": {"note": "private-note"}}}]}
KUBECONFIG = (
    "clusters:\n- cluster:\n    server: https://one.example.test\nusers:\n- user:\n    exec: {command: kubelogin}\n"
)


class ScriptedRunner:
    """Stands in for the real runner: answers a fixed script, and writes a kubeconfig as get-credentials does."""

    def __init__(self, clock: Clock) -> None:
        """Take 2.5 s of clock for each call."""
        self._clock = clock
        self._tops = collections.deque(["NAME CPU\nnode-one 120m\n", "NAME CPU\nnode-one 180m\n"])
        self.ended = False

    def run(self, argv: Sequence[str], timeout: float, overrides: Mapping[str, str]) -> Completed:
        """Answer argv from the script."""
        self._clock.sleep(2.5)
        program, verb = argv[0], argv[1]
        if program == "az":
            path = argv[-1]
            with open(path, "w", encoding="utf-8") as file:
                file.write(KUBECONFIG)
            return Completed(0, f"Merged as current context in {path}\n", "")
        if program == "kubectl" and verb == "get":
            return Completed(0, json.dumps(NODES), "")
        if program == "kubectl":
            return Completed(0, self._tops.popleft(), "")
        raise NotSignedIn("no AWS credentials; run aws configure or aws sso login")

    def end_children(self) -> None:
        """Note that the stop reached the real runner."""
        self.ended = True


class ScriptedHttp:
    """Stands in for the price list."""

    def get_json(self, url: str) -> object:
        """Give one price."""
        return {"Items": [{"retailPrice": 0.096}]}


class ScriptedPrompt:
    """Stands in for the person at the terminal, who answers yes."""

    def ask(self, question: str) -> bool:
        """Answer yes."""
        return True


def run_sample(runtime: Runtime) -> List[object]:
    """Make a short run's calls through runtime, as the assessment would, and give what came back."""
    kube = os.path.join(runtime.temp_root, "kube", "one.yaml")
    os.makedirs(os.path.dirname(kube), exist_ok=True)
    seen: List[object] = [runtime.runner.run(["az", "aks", "get-credentials", "--file", kube], 120, {})]
    with open(kube, encoding="utf-8") as file:
        seen.append(file.read())
    for verb in ("get", "top", "top"):
        seen.append(runtime.runner.run(["kubectl", verb, "nodes", "--kubeconfig", kube], 60, {}))
    try:
        runtime.runner.run(["aws", "sts", "get-caller-identity"], 60, {})
    except NotSignedIn as error:
        seen.append(str(error))
    seen.append(runtime.http.get_json(PRICES))
    seen.append(runtime.prompt.ask("Read disk use inside the VMs? [y/N] "))
    return seen


class RecordingTest(unittest.TestCase):
    """A recording of the sample run, made in one temporary folder and replayed in another."""

    def setUp(self) -> None:
        """Record the sample run on scripted programs, in a temporary folder of its own."""
        self.recorded_root = self.folder()
        real = FakeRun(FACTS, self.recorded_root)
        self.real_runner = ScriptedRunner(real.clock)
        self.recorder = Recorder(
            ["assess"],
            real.runtime(self.real_runner, ScriptedHttp(), ScriptedPrompt()),
            Redactor(),
        )
        self.real_answers = run_sample(self.recorder.runtime)

    def folder(self) -> str:
        """Make a temporary folder that the test removes afterwards."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return folder.name

    def test_the_run_gets_the_real_answers_while_recording(self) -> None:
        """Recording changes nothing the run sees."""
        self.assertEqual(self.real_answers[1], KUBECONFIG)
        self.assertEqual(self.real_answers[2], Completed(0, json.dumps(NODES), ""))

    def test_a_recording_survives_saving_and_loading(self) -> None:
        """A saved recording loads as the same recording."""
        path = os.path.join(self.folder(), "run.json.gz")
        save(self.recorder.recording, path)
        self.assertEqual(load(path), self.recorder.recording)

    def test_the_replay_gives_the_recorded_answers_on_another_temp_folder(self) -> None:
        """Replayed in a new temporary folder, the run gets the recorded answers, its files and its times back."""
        path = os.path.join(self.folder(), "run.json.gz")
        save(self.recorder.recording, path)
        recording = load(path)
        root = self.folder()
        run = FakeRun(recording.facts, root)
        runtime = run.runtime(
            ReplayRunner(recording.calls, root, run.clock),
            ReplayHttp(recording.fetches),
            ReplayPrompt(recording.answers),
        )
        kube = os.path.join(root, "kube", "one.yaml")
        self.assertEqual(
            run_sample(runtime),
            [
                Completed(0, f"Merged as current context in {kube}\n", ""),
                "# kubeconfig redacted while recording; its user signs in through kubelogin\n",
                Completed(0, json.dumps({"items": [{"metadata": {"name": "node-one"}}], "kind": "NodeList"}), ""),
                Completed(0, "NAME CPU\nnode-one 120m\n", ""),
                Completed(0, "NAME CPU\nnode-one 180m\n", ""),
                "no AWS credentials; run aws configure or aws sso login",
                {"Items": [{"retailPrice": 0.096}]},
                True,
            ],
        )
        self.assertEqual(run.clock.monotonic(), 12.5)

    def test_nothing_private_is_saved(self) -> None:
        """The saved file holds no kubeconfig server, no field off the allowlist and no temporary folder."""
        path = os.path.join(self.folder(), "run.json.gz")
        save(self.recorder.recording, path)
        with gzip.open(path, "rt", encoding="utf-8") as file:
            saved = file.read()
        for private in ("one.example.test", "private-note", json.dumps(self.recorded_root)[1:-1]):
            self.assertNotIn(private, saved)

    def test_a_failed_call_keeps_its_type_and_reason(self) -> None:
        """A typed failure is recorded as its type's name and its reason."""
        failed = [call for call in self.recorder.recording.calls if isinstance(call.outcome, Failure)]
        self.assertEqual(
            [call.outcome for call in failed],
            [Failure("NotSignedIn", "no AWS credentials; run aws configure or aws sso login")],
        )

    def test_each_call_keeps_its_time(self) -> None:
        """Each call keeps how long it took on the run's clock."""
        self.assertEqual([call.seconds for call in self.recorder.recording.calls], [2.5] * 5)

    def test_the_stop_reaches_the_real_runner(self) -> None:
        """Ending the children while recording ends the real runner's children."""
        self.recorder.runtime.runner.end_children()
        self.assertTrue(self.real_runner.ended)

    @unittest.skipUnless(os.name == "posix", "file modes are POSIX")
    def test_the_file_is_the_users_alone(self) -> None:
        """A saved recording is mode 600."""
        path = os.path.join(self.folder(), "run.json.gz")
        save(self.recorder.recording, path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_a_recording_never_replaces_a_file(self) -> None:
        """Saving to a path that exists fails, and the file there stays as it was."""
        path = os.path.join(self.folder(), "run.json.gz")
        with open(path, "w", encoding="utf-8") as file:
            file.write("kept")
        with self.assertRaises(FileExistsError):
            save(self.recorder.recording, path)
        with open(path, encoding="utf-8") as file:
            self.assertEqual(file.read(), "kept")


def pod_page_text(items: List[Json], token: str = "") -> str:
    """Write a page of pods as the API server gives it."""
    return json.dumps({"kind": "PodList", "apiVersion": "v1", "metadata": {"continue": token}, "items": items})


def labels_in(call: Call) -> Json:
    """Give the labels of the one pod on a recorded page."""
    assert isinstance(call.outcome, Completed)
    return json.loads(call.outcome.stdout)["items"][0]["metadata"]["labels"]


class PodPagesRunner:
    """Stands in for kubectl: answers each cluster's pod lists from its pages, in order."""

    def __init__(self, pages: Mapping[str, List[str]]) -> None:
        """Answer with each cluster's pages, by its kubeconfig's file name."""
        self._pages: Dict[str, Deque[str]] = {name: collections.deque(texts) for name, texts in pages.items()}

    def run(self, argv: Sequence[str], timeout: float, overrides: Mapping[str, str]) -> Completed:
        """Give the next page of argv's cluster."""
        name = os.path.basename(argv[list(argv).index("--kubeconfig") + 1])
        return Completed(0, self._pages[name].popleft(), "")

    def end_children(self) -> None:
        """End nothing: no program was started."""


class FinishedTest(unittest.TestCase):
    """Recorder.finished."""

    def test_each_clusters_pod_pages_are_narrowed_together(self) -> None:
        """A label that a term on one page uses stays on its cluster's other page, and goes in another cluster."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        labels = {"app": "web", "team": "one"}
        spread = {"topologySpreadConstraints": [SPREAD]}
        pages = {
            "one.yaml": [
                pod_page_text([pod("shop", "web-one", "node-one", [container("app")], labels=labels)], "page1="),
                pod_page_text([pod("shop", "web-two", "node-one", [container("app")], labels=labels, spec=spread)]),
            ],
            "two.yaml": [pod_page_text([pod("shop", "web-three", "node-two", [container("app")], labels=labels)])],
        }
        run = FakeRun(FACTS, folder.name)
        recorder = Recorder(
            ["assess"], run.runtime(PodPagesRunner(pages), ScriptedHttp(), ScriptedPrompt()), Redactor()
        )
        token: Optional[str]
        for name, token in (("one.yaml", None), ("one.yaml", "page1="), ("two.yaml", None)):
            query = "limit=500" if token is None else f"limit=500&continue={token}"
            kube = os.path.join(folder.name, "kube", name)
            recorder.runtime.runner.run(
                ["kubectl", "--kubeconfig", kube, "get", "--raw", f"/api/v1/pods?{query}"], 60, {}
            )
        self.assertEqual([labels_in(call) for call in recorder.recording.calls], [labels] * 3)
        finished = recorder.finished()
        self.assertEqual([labels_in(call) for call in finished.calls], [{"app": "web"}, {"app": "web"}, {}])
        self.assertEqual([call.argv for call in finished.calls], [call.argv for call in recorder.recording.calls])


class KeyTest(unittest.TestCase):
    """call_key."""

    def setUp(self) -> None:
        """Make two temporary folders, as two runs would."""
        self.roots = []
        for _ in range(2):
            folder = tempfile.TemporaryDirectory()
            self.addCleanup(folder.cleanup)
            self.roots.append(folder.name)

    def test_temp_names_are_the_same_on_two_runs(self) -> None:
        """The same call in two runs, each with its own temporary folder, has one key, written with /."""
        keys = [
            call_key(["kubectl", "--kubeconfig", os.path.join(root, "kube", "one.yaml")], root) for root in self.roots
        ]
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(json.loads(keys[0]), ["kubectl", "--kubeconfig", "<temp>/kube/one.yaml"])

    def test_an_az_file_argument_is_keyed_by_its_text(self) -> None:
        """An az @file argument is keyed by the file's text, as the same value is keyed where no file is used."""
        root = self.roots[0]
        query = os.path.join(root, "query.txt")
        with open(query, "w", encoding="utf-8") as file:
            file.write("Resources | where type =~ 'microsoft.compute/virtualmachines'")
        self.assertEqual(
            call_key(["az", "graph", "query", "-q", "@" + query], root),
            call_key(
                ["az", "graph", "query", "-q", "Resources | where type =~ 'microsoft.compute/virtualmachines'"], root
            ),
        )

    def test_other_arguments_are_kept_as_they_are(self) -> None:
        """Arguments outside the temporary folder keep their text, separators included."""
        argv = ["az", "rest", "--url", "/subscriptions?api-version=2022-12-01", "--query", "value[].id"]
        self.assertEqual(json.loads(call_key(argv, self.roots[0])), argv)


def case_insensitive(folder: str) -> bool:
    """Tell whether the file system under folder ignores case in names."""
    probe = os.path.join(folder, "CaseProbe")
    os.mkdir(probe)
    return os.path.isdir(os.path.join(folder, "caseprobe"))


class RefuseInsideTest(unittest.TestCase):
    """refuse_inside, with a stand-in repository in a temporary folder."""

    def setUp(self) -> None:
        """Make a folder holding the stand-in repository and an outside folder."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.top = os.path.realpath(folder.name)
        self.repo = os.path.join(self.top, "Repository")
        os.makedirs(os.path.join(self.repo, "tests"))

    def test_a_path_inside_this_repository_is_refused(self) -> None:
        """The real repository is the default, and a path inside it is refused."""
        with self.assertRaises(RecordingRefused):
            refuse_inside(os.path.join(REPO, "tests", "run.json.gz"))

    def test_a_path_inside_is_refused_even_under_new_folders(self) -> None:
        """A path under folders that don't exist yet is refused when they would be inside."""
        with self.assertRaises(RecordingRefused):
            refuse_inside(os.path.join(self.repo, "new", "deeper", "run.json.gz"), self.repo)

    def test_a_path_outside_is_accepted(self) -> None:
        """A path beside the repository, even one that starts with its name, is accepted."""
        refuse_inside(os.path.join(self.top, "recordings", "run.json.gz"), self.repo)
        refuse_inside(os.path.join(self.top, "Repository-recordings", "run.json.gz"), self.repo)

    def test_a_case_changed_path_is_refused(self) -> None:
        """On a file system that ignores case, the repository spelt in other case is still the repository."""
        if not case_insensitive(self.top):
            self.skipTest("this file system tells case apart")
        with self.assertRaises(RecordingRefused):
            refuse_inside(os.path.join(self.top, "REPOSITORY", "tests", "run.json.gz"), self.repo)

    @unittest.skipIf(sys.platform == "win32", "making a symlink needs extra rights on Windows")
    def test_a_symlinked_path_is_refused(self) -> None:
        """A link to the repository, or to a folder in it, is the repository, .. included."""
        os.symlink(self.repo, os.path.join(self.top, "link"))
        os.symlink(os.path.join(self.repo, "tests"), os.path.join(self.top, "tests-link"))
        for path in (os.path.join("link", "run.json.gz"), os.path.join("tests-link", "..", "run.json.gz")):
            with self.subTest(path=path), self.assertRaises(RecordingRefused):
                refuse_inside(os.path.join(self.top, path), self.repo)

    def test_the_refusal_names_the_path(self) -> None:
        """The refusal says which path and why."""
        path = os.path.join(self.repo, "run.json.gz")
        with self.assertRaisesRegex(RecordingRefused, "is inside the repository; write recordings outside it"):
            refuse_inside(path, self.repo)
