"""Replaying recorded calls, fetches and answers."""

import json
import os
import tempfile
import unittest
from typing import List

from tests.replay.fake_cloud import VirtualClock
from tests.replay.recording import Body, Call, Failure, Fetch
from tests.replay.replay import ReplayError, ReplayHttp, ReplayPrompt, ReplayRunner
from tests.replay.scenarios import FACTS
from zoorik.errors import CommandTimedOut, PriceListUnreachable
from zoorik.system.process import Completed

TOP = ["kubectl", "top", "pods", "--all-namespaces"]
PRICES = "https://prices.example.test/api/retail/prices"


def answered(argv: List[str], stdout: str) -> Call:
    """Give a recorded call of argv that answered stdout after 2 s; argv holds no temporary paths."""
    return Call(json.dumps(argv), argv, Completed(0, stdout, ""), {}, 2.0)


class ReplayRunnerTest(unittest.TestCase):
    """ReplayRunner, on a temporary folder of its own."""

    def setUp(self) -> None:
        """Make the temporary folder and the clock."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = folder.name
        self.clock = VirtualClock(FACTS.started)

    def test_each_key_answers_in_order_and_never_twice(self) -> None:
        """Repeats of one call get the recorded answers in order; one more fails the replay, naming the key."""
        runner = ReplayRunner([answered(TOP, "first\n"), answered(TOP, "second\n")], self.root, self.clock)
        self.assertEqual(runner.run(TOP, 60, {}).stdout, "first\n")
        self.assertEqual(runner.run(TOP, 60, {}).stdout, "second\n")
        with self.assertRaisesRegex(ReplayError, "no recorded answer left for .*kubectl.*top"):
            runner.run(TOP, 60, {})

    def test_a_call_nobody_recorded_fails_the_replay(self) -> None:
        """A call with no recorded key fails the replay."""
        with self.assertRaisesRegex(ReplayError, "account"):
            ReplayRunner([], self.root, self.clock).run(["az", "account", "show"], 60, {})

    def test_a_written_file_is_recreated_in_this_runs_folder(self) -> None:
        """A file the call wrote is written again under this run's temporary folder, folders included."""
        argv = ["az", "aks", "get-credentials", "--file", os.path.join(self.root, "kube", "one.yaml")]
        call = Call(
            json.dumps(["az", "aks", "get-credentials", "--file", "<temp>/kube/one.yaml"]),
            argv,
            Completed(0, "Merged as current context in <temp>/kube/one.yaml\n", ""),
            {"<temp>/kube/one.yaml": "# kubeconfig redacted while recording\n"},
            3.0,
        )
        completed = ReplayRunner([call], self.root, self.clock).run(argv, 60, {})
        with open(os.path.join(self.root, "kube", "one.yaml"), encoding="utf-8") as file:
            self.assertEqual(file.read(), "# kubeconfig redacted while recording\n")
        self.assertEqual(completed.stdout, f"Merged as current context in {self.root}/kube/one.yaml\n")

    def test_a_recorded_failure_is_raised_again_as_its_type(self) -> None:
        """A recorded typed failure comes back as the same type, with the same reason."""
        timed_out = Failure("CommandTimedOut", "no answer from az within 120 s")
        runner = ReplayRunner([Call(json.dumps(TOP), TOP, timed_out, {}, 120.0)], self.root, self.clock)
        with self.assertRaisesRegex(CommandTimedOut, "^no answer from az within 120 s$"):
            runner.run(TOP, 120, {})

    def test_the_calls_a_run_never_made_are_named(self) -> None:
        """Each recorded call left over is named by its key, once for each answer left."""
        runner = ReplayRunner([answered(TOP, "first\n"), answered(TOP, "second\n")], self.root, self.clock)
        self.assertEqual(runner.unused(), [json.dumps(TOP), json.dumps(TOP)])
        runner.run(TOP, 60, {})
        runner.run(TOP, 60, {})
        self.assertEqual(runner.unused(), [])

    def test_each_call_takes_its_recorded_time(self) -> None:
        """The clock moves on by each call's recorded time."""
        runner = ReplayRunner([answered(TOP, "first\n"), answered(TOP, "second\n")], self.root, self.clock)
        runner.run(TOP, 60, {})
        runner.run(TOP, 60, {})
        self.assertEqual(self.clock.monotonic(), 4.0)


class ReplayHttpAndPromptTest(unittest.TestCase):
    """ReplayHttp and ReplayPrompt."""

    def test_fetches_answer_in_order(self) -> None:
        """Each URL's answers come in order, a recorded failure is raised again, and one more fails the replay."""
        http = ReplayHttp(
            [
                Fetch(PRICES, Body({"Items": [{"retailPrice": 0.1}]})),
                Fetch(PRICES, Failure("PriceListUnreachable", "the public price list is not reachable")),
            ]
        )
        self.assertEqual(http.get_json(PRICES), {"Items": [{"retailPrice": 0.1}]})
        with self.assertRaisesRegex(PriceListUnreachable, "^the public price list is not reachable$"):
            http.get_json(PRICES)
        with self.assertRaises(ReplayError):
            http.get_json(PRICES)

    def test_fetches_and_answers_left_over_are_named(self) -> None:
        """A fetch the run never made is named by its URL, and an answer no question took is named too."""
        http = ReplayHttp([Fetch(PRICES, Body({"Items": []}))])
        prompt = ReplayPrompt([False])
        self.assertEqual((http.unused(), prompt.unused()), ([PRICES], ["y/N"]))
        http.get_json(PRICES)
        prompt.ask("Read disk use inside the VMs? [y/N] ")
        self.assertEqual((http.unused(), prompt.unused()), ([], []))

    def test_answers_come_in_order(self) -> None:
        """The y/N answers come in order, no terminal included, and one more question fails the replay."""
        prompt = ReplayPrompt([True, None])
        self.assertIs(prompt.ask("Read disk use inside the VMs? [y/N] "), True)
        self.assertIsNone(prompt.ask("Read disk use inside the VMs? [y/N] "))
        with self.assertRaises(ReplayError):
            prompt.ask("Read disk use inside the VMs? [y/N] ")
