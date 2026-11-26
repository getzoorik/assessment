"""Record a real run, or rewrite the goldens.

python -m tests.replay record --out FILE -- zoorik assess [options]
    Runs the command for real, against the clouds you are signed in to, and writes its recording, redacted, to FILE,
    which must be outside the repository.
python -m tests.replay update
    Runs the tests, rewriting every golden from what this code gives.
"""

import argparse
import importlib
import os
import sys
import unittest
from typing import List, Sequence, Tuple

from tests.replay.golden import UPDATE
from tests.replay.recording import REPO, Recorder, refuse_inside, save
from tests.replay.redact import Redactor
from zoorik.pricing.http import UrllibHttpClient
from zoorik.system.clock import SystemClock
from zoorik.system.runtime import real_runtime

COMMAND = ["zoorik", "assess"]


def main(argv: Sequence[str]) -> int:
    """Run record or update with argv, and give the exit code."""
    own, command = split(argv)
    parser = argparse.ArgumentParser(
        prog="python -m tests.replay", description="Record a real run, or rewrite goldens."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    record_command = commands.add_parser("record", help="record a real run of zoorik assess, given after --")
    record_command.add_argument("--out", required=True, help="the recording's file, outside the repository")
    commands.add_parser("update", help="run the tests, rewriting every golden")
    args = parser.parse_args(own)
    if args.command == "record":
        return record(args.out, command)
    return update()


def split(argv: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Split argv at --, into this tool's own arguments and the command to record."""
    if "--" not in argv:
        return list(argv), []
    at = list(argv).index("--")
    return list(argv[:at]), list(argv[at + 1 :])


def record(out: str, command: List[str]) -> int:
    """Run command, which is zoorik assess and its options, for real, and save its recording to out."""
    if command[:2] != COMMAND:
        raise SystemExit("record runs the command given after --, which is zoorik assess and its options")
    refuse_inside(out)
    if os.path.lexists(out):
        raise SystemExit(f"{out} exists; a recording never replaces a file")
    recorder, code = run_for_real(command[1:])
    save(recorder.recording, out)
    return code


def run_for_real(argv: List[str]) -> Tuple[Recorder, int]:
    """Run zoorik assess with argv against the clouds you are signed in to, recording it; give the exit code too."""
    run = importlib.import_module("zoorik.cli").run
    recorder = Recorder(argv, real_runtime(UrllibHttpClient(SystemClock())), Redactor(kubectl_lists={}))
    code: int = run(argv, recorder.runtime)
    return recorder, code


def update() -> int:
    """Run every test, rewriting the goldens, and give 0 when all of them pass."""
    os.environ[UPDATE] = "1"
    argv = ["python -m tests.replay update", "discover", "-s", os.path.join(REPO, "tests"), "-t", REPO]
    program = unittest.main(module=None, argv=argv, exit=False)
    return 0 if program.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
