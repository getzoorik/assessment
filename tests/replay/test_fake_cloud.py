"""The fakes a run under test gets."""

import json
import signal
import threading
import unittest
from datetime import timedelta
from typing import List

from tests.replay.fake_cloud import (
    CapturingConsole,
    FakeCloud,
    FakeRun,
    Stopped,
    StopSwitch,
    VirtualClock,
    exit_at_once,
)
from tests.replay.recording import Call
from tests.replay.replay import ReplayError, ReplayHttp, ReplayPrompt
from tests.replay.scenarios import FACTS, NO_CLI, Scenario
from zoorik.errors import ToolNotInstalled
from zoorik.system.process import Completed

ACCOUNT = ["az", "account", "show"]


class VirtualClockTest(unittest.TestCase):
    """VirtualClock."""

    def test_time_moves_only_when_a_thread_waits_and_only_for_it(self) -> None:
        """Each thread starts at the run's start, and its waits move its own time alone."""
        clock = VirtualClock(FACTS.started)
        self.assertEqual(clock.now(), FACTS.started)
        clock.sleep(5)
        seen: List[float] = []

        def other() -> None:
            seen.append(clock.monotonic())
            clock.sleep(60)
            seen.append(clock.monotonic())

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
        self.assertEqual(seen, [0.0, 60.0])
        self.assertEqual(clock.monotonic(), 5.0)
        self.assertEqual(clock.now(), FACTS.started + timedelta(seconds=5))


class CapturingConsoleTest(unittest.TestCase):
    """CapturingConsole."""

    def test_each_part_keeps_its_own_lines(self) -> None:
        """Lines are kept by part, the run's own under run, and held lines join their part after the question."""
        console = CapturingConsole()
        kubernetes = console.part("Kubernetes")
        disks = console.part("Disks")
        console.line("Zoorik cloud cost assessment.")
        with disks.question():
            kubernetes.line("Read cluster one.")
            self.assertEqual(console.lines, {"run": ["Zoorik cloud cost assessment."]})
        disks.line("Read 3 VMs.")
        self.assertEqual(
            console.lines,
            {"run": ["Zoorik cloud cost assessment."], "Kubernetes": ["Read cluster one."], "Disks": ["Read 3 VMs."]},
        )


class StopTest(unittest.TestCase):
    """exit_at_once, Stopped and StopSwitch."""

    def test_the_fake_exit_is_not_an_error_the_run_catches(self) -> None:
        """The fake exit raises Stopped with the code; Stopped is no Exception, so the run's handlers let it pass."""
        with self.assertRaises(Stopped) as stopped:
            exit_at_once(130)
        self.assertEqual(stopped.exception.code, 130)
        self.assertFalse(issubclass(Stopped, Exception))

    def test_the_switch_fires_the_installed_handler(self) -> None:
        """Firing the switch calls the handler the run installed, with the signal's number."""
        switch = StopSwitch()
        with self.assertRaisesRegex(AssertionError, "no stop handler"):
            switch.fire(signal.SIGINT)
        received: List[int] = []
        switch.install(received.append)
        switch.fire(signal.SIGINT)
        self.assertEqual(received, [signal.SIGINT])


class FakeRunTest(unittest.TestCase):
    """FakeRun and FakeCloud."""

    def test_the_runtime_carries_the_facts_and_the_fakes(self) -> None:
        """The Runtime takes the facts, the temporary folder, the fake exit and the stop switch."""
        run = FakeRun(FACTS, "/temp/run")
        runtime = run.runtime(FakeCloud(NO_CLI, run.temp_root, run.clock), ReplayHttp([]), ReplayPrompt([]))
        self.assertEqual((runtime.salt, runtime.windows, runtime.env), (FACTS.salt, FACTS.windows, FACTS.env))
        self.assertEqual(runtime.temp_root, "/temp/run")
        self.assertIs(runtime.clock, run.clock)
        self.assertIs(runtime.console, run.console)
        self.assertIs(runtime.exit_now, exit_at_once)
        received: List[int] = []
        runtime.install_stop(received.append)
        run.stop.fire(signal.SIGTERM)
        self.assertEqual(received, [signal.SIGTERM])

    def test_a_program_the_scenario_lacks_is_not_installed(self) -> None:
        """With neither cloud CLI installed, az and aws are not installed."""
        cloud = FakeCloud(NO_CLI, "/temp/run", VirtualClock(FACTS.started))
        for tool in ("az", "aws"):
            with self.subTest(tool=tool), self.assertRaisesRegex(ToolNotInstalled, f"^{tool} is not installed$"):
                cloud.run([tool, "--version"], 30, {})

    def test_an_installed_program_answers_from_the_scenario(self) -> None:
        """An installed program answers from the scenario's calls, and a call it lacks fails the replay."""
        call = Call(json.dumps(ACCOUNT), ACCOUNT, Completed(0, '{"tenantId": "t-1"}', ""), {}, 1.0)
        scenario = Scenario("signed-in", FACTS, frozenset({"az"}), [call], [], [])
        cloud = FakeCloud(scenario, "/temp/run", VirtualClock(FACTS.started))
        self.assertEqual(cloud.run(ACCOUNT, 30, {}), Completed(0, '{"tenantId": "t-1"}', ""))
        with self.assertRaises(ReplayError):
            cloud.run(["az", "account", "list"], 30, {})
