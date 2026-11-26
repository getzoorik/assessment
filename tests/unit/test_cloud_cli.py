"""The one way az and aws run."""

import json
import os
from datetime import datetime, timezone

from tests.replay.scenarios import call
from tests.unit.clis import CloudTestCase, answer, aws, az, failing
from zoorik.cloud.aws import AWS, NOT_DELIVERED
from zoorik.cloud.azure import AZURE
from zoorik.cloud.cli import THROTTLE_WAITS, CloudCli, kubeconfig_file, summary, utc_text
from zoorik.errors import AccessDenied, CommandFailed, NotSignedIn, Throttled, UnexpectedResponse

THROTTLED = "ERROR: (TooManyRequests) The request is being throttled."
BODY = json.dumps({"query": "resources | where name == 'one'"})


class CallTest(CloudTestCase):
    """Every call: its options, its environment, the throttle retry and the typed failures."""

    def test_every_call_ends_with_the_output_options_and_gets_its_overrides(self) -> None:
        """An az call ends with JSON output and no warnings, telemetry off; an aws call with JSON output, no pager."""
        host, runner = self.host([answer(az("account", "show"), {}), answer(aws("sts", "get-caller-identity"), {})])
        CloudCli(AZURE, host).read(["az", "account", "show"])
        CloudCli(AWS, host).read(["aws", "sts", "get-caller-identity"], timeout=30)
        self.assertEqual(
            runner.runs,
            [
                (
                    ["az", "account", "show", "-o", "json", "--only-show-errors"],
                    120.0,
                    {"AZURE_CORE_COLLECT_TELEMETRY": "false"},
                ),
                (["aws", "sts", "get-caller-identity", "--output", "json"], 30.0, {"AWS_PAGER": ""}),
            ],
        )

    def test_a_call_throttled_twice_is_answered_on_the_third_try(self) -> None:
        """A throttled call waits 5 s, then 10 s, and gives the third answer."""
        argv = az("account", "list")
        host, runner = self.host([failing(argv, THROTTLED), failing(argv, THROTTLED), answer(argv, [])])
        self.assertEqual(CloudCli(AZURE, host).read(["az", "account", "list"]), [])
        self.assertEqual(len(runner.runs), 3)
        self.assertEqual(self.clock.monotonic(), sum(THROTTLE_WAITS))
        self.assertEqual(THROTTLE_WAITS, (5.0, 10.0))

    def test_a_call_throttled_three_times_is_throttled(self) -> None:
        """After the third throttled try the call fails as Throttled, with no wait after the last try."""
        argv = aws("ec2", "describe-volumes", "--region", "eu-west-1")
        error = "An error occurred (RequestLimitExceeded) when calling the DescribeVolumes operation"
        host, runner = self.host([failing(argv, error)] * 3)
        with self.assertRaisesRegex(Throttled, r"^AWS is throttling requests; try again later$"):
            CloudCli(AWS, host).read(["aws", "ec2", "describe-volumes", "--region", "eu-west-1"])
        self.assertEqual(len(runner.runs), 3)
        self.assertEqual(self.clock.monotonic(), 15.0)

    def test_the_first_matching_row_names_the_failure(self) -> None:
        """Rows are tried in order: the Run Command permission comes before plain access denied."""
        cli = CloudCli(AZURE, self.host([])[0])
        error = cli.failure("ERROR: (AuthorizationFailed) no runCommand/action permission on the VM")
        self.assertIsInstance(error, AccessDenied)
        self.assertIn("Microsoft.Compute/virtualMachines/runCommand/action", str(error))
        self.assertEqual(str(cli.failure("(AuthorizationFailed) The client has no access")), "access denied")
        self.assertIsInstance(cli.failure("ERROR: Please run 'az login' to setup account."), NotSignedIn)

    def test_an_error_no_row_names_is_a_short_reason_without_names(self) -> None:
        """An unnamed error gives its first line, without the error prefix or quoted names."""
        cli = CloudCli(AWS, self.host([])[0])
        error = cli.failure("\nerror: the volume 'data-one' is not in \"zone-one\"\nmore detail")
        self.assertIsInstance(error, CommandFailed)
        self.assertEqual(str(error), "the volume ... is not in ...")

    def test_the_error_comes_from_stderr_alone(self) -> None:
        """A failed call is named from its stderr alone; with nothing there, the error is unknown."""
        argv = aws("sts", "get-caller-identity", "--region", "us-east-1")
        host, _ = self.host([call(argv, "Unable to locate credentials", exit_code=255, stderr=" \n")])
        with self.assertRaisesRegex(CommandFailed, "^unknown error$"):
            CloudCli(AWS, host).read(["aws", "sts", "get-caller-identity", "--region", "us-east-1"])

    def test_a_malformed_answer_is_unexpected(self) -> None:
        """An answer that is not JSON raises UnexpectedResponse."""
        host, _ = self.host([call(az("account", "show"), "Welcome to az")])
        with self.assertRaisesRegex(UnexpectedResponse, r"^unexpected answer: az gave no JSON$"):
            CloudCli(AZURE, host).read(["az", "account", "show"])

    def test_run_needs_no_answer_and_fails_like_read(self) -> None:
        """A call that answers with no JSON ignores what it prints, and its failure is named as a read's is."""
        argv = az("aks", "get-credentials", "--name", "one")
        host, _ = self.host([call(argv, "Merged as current context"), failing(argv, "ERROR: (ResourceNotFound) gone")])
        cli = CloudCli(AZURE, host)
        cli.run(["az", "aks", "get-credentials", "--name", "one"])
        with self.assertRaisesRegex(CommandFailed, r"^not found$"):
            cli.run(["az", "aks", "get-credentials", "--name", "one"])

    def test_read_unless_gives_none_for_its_reason_only(self) -> None:
        """read_unless gives None when the call fails with the reason given, and raises any other failure."""
        argv = aws("ssm", "get-command-invocation", "--region", "eu-west-1")
        absent = "An error occurred (InvocationDoesNotExist) when calling the GetCommandInvocation operation"
        denied = "An error occurred (AccessDeniedException): not authorized to perform ssm:GetCommandInvocation"
        host, _ = self.host([failing(argv, absent), failing(argv, denied), answer(argv, {"Status": "Success"})])
        cli = CloudCli(AWS, host)
        command = ["aws", "ssm", "get-command-invocation", "--region", "eu-west-1"]
        self.assertIsNone(cli.read_unless(command, NOT_DELIVERED))
        with self.assertRaisesRegex(AccessDenied, r"ssm:GetCommandInvocation"):
            cli.read_unless(command, NOT_DELIVERED)
        self.assertEqual(cli.read_unless(command, NOT_DELIVERED), {"Status": "Success"})


class WindowsArgumentsTest(CloudTestCase):
    """On Windows, az values that cmd.exe would read again go to az in files."""

    def test_a_value_with_special_characters_goes_in_a_file(self) -> None:
        """The runner gets @file for the value, the file holds the value exactly, and it is gone after the call."""
        argv = az("rest", "--method", "post", "--body", BODY)
        host, runner = self.host([answer(argv, {"data": []})], windows=True)
        CloudCli(AZURE, host).read(["az", "rest", "--method", "post", "--body", BODY])
        seen = runner.argvs[0]
        self.assertEqual(seen[:5], ["az", "rest", "--method", "post", "--body"])
        self.assertTrue(seen[5].startswith("@" + self.temp_root + os.sep))
        self.assertEqual(seen[6:], list(AZURE.output))
        self.assertEqual(runner.arg_files, {seen[5]: BODY})
        self.assertFalse(os.path.exists(seen[5][1:]))

    def test_files_are_for_az_on_windows_only(self) -> None:
        """Calls to aws on Windows, and to az elsewhere, get every value as it is."""
        windows, windows_runner = self.host([answer(aws("ec2", "describe-regions", "--query", BODY), [])], windows=True)
        CloudCli(AWS, windows).read(["aws", "ec2", "describe-regions", "--query", BODY])
        other, other_runner = self.host([answer(az("rest", "--body", BODY), {})])
        CloudCli(AZURE, other).read(["az", "rest", "--body", BODY])
        self.assertIn(BODY, windows_runner.argvs[0])
        self.assertIn(BODY, other_runner.argvs[0])

    def test_a_failed_call_removes_its_files_too(self) -> None:
        """The files go however the call ends."""
        argv = az("rest", "--body", BODY)
        host, runner = self.host([failing(argv, "ERROR: (AuthorizationFailed) no access")], windows=True)
        with self.assertRaises(AccessDenied):
            CloudCli(AZURE, host).read(["az", "rest", "--body", BODY])
        self.assertEqual(list(runner.arg_files.values()), [BODY])
        self.assertEqual([name for name in os.listdir(self.temp_root) if name.startswith("arg-")], [])


class HelpersTest(CloudTestCase):
    """summary, utc_text and kubeconfig_file."""

    def test_summary_is_one_short_line(self) -> None:
        """An empty error is unknown, and a long first line is shortened to 100 characters."""
        self.assertEqual(summary(" \n "), "unknown error")
        self.assertEqual(summary("ERROR: " + "x" * 300), "x" * 100)

    def test_times_are_written_as_the_apis_take_them(self) -> None:
        """A UTC time is written to the second, with a Z."""
        self.assertEqual(utc_text(datetime(2026, 1, 5, 9, 30, 15, 999, tzinfo=timezone.utc)), "2026-01-05T09:30:15Z")

    def test_a_kubeconfig_name_is_the_same_on_every_run(self) -> None:
        """The same cluster gets the same file in the kube folder; another cluster gets another file."""
        one = kubeconfig_file(self.temp_root, ("aks", "sub-one", "group-one", "cluster-one"))
        self.assertEqual(one, kubeconfig_file(self.temp_root, ("aks", "sub-one", "group-one", "cluster-one")))
        self.assertNotEqual(one, kubeconfig_file(self.temp_root, ("aks", "sub-one", "group-one", "cluster-two")))
        self.assertEqual(os.path.dirname(one), os.path.join(self.temp_root, "kube"))
        self.assertTrue(os.path.isdir(os.path.dirname(one)))
        self.assertRegex(os.path.basename(one), r"^[0-9a-f]{16}\.yaml$")
