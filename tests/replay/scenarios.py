"""Made-up runs for replay tests, with neutral names, and the made-up estate both parts read.

The estate is one subscription of one directory and one account with one enabled region. The builders give each az
and aws call as its reader runs it, so a scenario's answers are found by the same keys a recording holds.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from tests.replay.recording import Call, Fetch, RunFacts
from zoorik.cloud import aws as aws_cloud
from zoorik.cloud import azure as azure_cloud
from zoorik.cloud.aws import AwsScope
from zoorik.cloud.azure import AzureScope, Subscription
from zoorik.system.env import EnvFacts
from zoorik.system.process import Completed

Json = Dict[str, object]


@dataclass(frozen=True)
class Scenario:
    """A made-up run: its facts, the programs installed, and the answers to its calls, fetches and questions."""

    name: str
    facts: RunFacts
    installed: FrozenSet[str]
    calls: Sequence[Call]
    fetches: Sequence[Fetch]
    answers: Sequence[Optional[bool]]


# A Monday morning, outside Cloud Shell, with a fixed salt so anonymized names are the same on every run.
FACTS = RunFacts(
    started=datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc),
    salt="0123456789abcdef",
    windows=False,
    env=EnvFacts(None, None, None),
)

# Neither cloud CLI is installed.
NO_CLI = Scenario(name="no-cli", facts=FACTS, installed=frozenset(), calls=(), fetches=(), answers=())

SUB = "sub-one"
TENANT = "tenant-one"
REGION = "westeurope"
GROUP = "group-one"
AZURE_SCOPE = AzureScope("Directory One (one.example.test)", (Subscription(SUB, "subscription-one", TENANT),), ())
ACCOUNT = "123456789012"
EU = "eu-west-1"
US = "us-east-1"
AWS_SCOPE = AwsScope(ACCOUNT, (EU,))


def call(argv: Sequence[str], stdout: str = "", *, exit_code: int = 0, stderr: str = "") -> Call:
    """Make a made-up call and its answer: argv as the program gets it, with the temporary folder as <temp>."""
    return Call(
        key=json.dumps(list(argv)),
        argv=list(argv),
        outcome=Completed(exit_code, stdout, stderr),
        files={},
        seconds=0.0,
    )


def az_call(args: Sequence[str], value: object) -> Call:
    """Make an az call as the reader runs it, answered with value as JSON."""
    return call(["az", *args, *azure_cloud.AZURE.output], json.dumps(value))


def az_failure(args: Sequence[str], stderr: str) -> Call:
    """Make an az call as the reader runs it, failing with stderr."""
    return call(["az", *args, *azure_cloud.AZURE.output], exit_code=1, stderr=stderr)


def aws_call(args: Sequence[str], value: object) -> Call:
    """Make an aws call as the reader runs it, answered with value as JSON."""
    return call(["aws", *args, *aws_cloud.AWS.output], json.dumps(value))


def aws_failure(args: Sequence[str], stderr: str) -> Call:
    """Make an aws call as the reader runs it, failing with stderr."""
    return call(["aws", *args, *aws_cloud.AWS.output], exit_code=255, stderr=stderr)


def instance(
    number: int, kind: str, volumes: Sequence[Tuple[str, str]], tags: Optional[Mapping[str, str]] = None
) -> Json:
    """Make a running Linux instance i-0<number>: its root volume, its data volumes by device and its tags."""
    mappings = [("/dev/xvda", f"vol-root-0{number}"), *volumes]
    record: Json = {
        "InstanceId": f"i-0{number}",
        "InstanceType": kind,
        "State": {"Code": 16, "Name": "running"},
        "RootDeviceName": "/dev/xvda",
        "BlockDeviceMappings": [
            {"DeviceName": device, "Ebs": {"VolumeId": volume, "Status": "attached"}} for device, volume in mappings
        ],
        "NetworkInterfaces": [{"NetworkInterfaceId": f"eni-0{number}"}],
    }
    if tags is not None:
        record["Tags"] = [{"Key": key, "Value": value} for key, value in tags.items()]
    return record


def instances_args(region: str) -> List[str]:
    """Give the aws arguments that read a region's instances."""
    return ["ec2", "describe-instances", "--filters", aws_cloud.LIVE_STATES, "--region", region]


def cluster_list(clusters: Sequence[str], region: str = EU) -> Call:
    """Make the region's EKS cluster list."""
    return aws_call(["eks", "list-clusters", "--region", region], {"clusters": list(clusters)})
