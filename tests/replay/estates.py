"""Made-up estates for the whole command: what each signed-in cloud covers, and what both parts read in it.

Each estate joins the Kubernetes and disk scenarios' answers under the scope reads that open a run, with the options
zoorik assess is run with. The shared reads (an AWS region's clusters, instances and instance types, and an Azure
region's Compute SKUs) are answered once, as the run makes them once for both parts.
"""

from dataclasses import dataclass, replace
from typing import List, Optional, Sequence, Tuple

from tests.replay.disks import (
    AWS_KERNEL,
    D8S,
    NITRO_TYPE,
    XEN_TYPE,
    azure_inventory,
    cloudwatch,
    ebs_prices,
    graph_args,
    guest_output,
    linux_vm,
    root_volume,
    skus,
    ssm_online,
    ssm_read,
    types_call,
    volume,
)
from tests.replay.kubernetes import (
    EC2_TYPES,
    PRIVATE,
    VM_TYPES,
    aks_list,
    aks_list_refused,
    aks_private,
    aks_public,
    aks_record,
    eks_node_kinds,
    instance_types,
)
from tests.replay.recording import Call, Fetch
from tests.replay.scenarios import (
    ACCOUNT,
    EU,
    FACTS,
    NO_CLI,
    SUB,
    TENANT,
    US,
    Scenario,
    aws_call,
    aws_failure,
    az_call,
    az_failure,
    instance,
)
from zoorik.cloud.azure import SUBSCRIPTIONS_QUERY
from zoorik.kubectl.usage import SampleWindow
from zoorik.system.env import EnvFacts

# The default window: 5 minutes, so a cluster read directly answers eleven kubectl top samples.
FIVE_MINUTES = SampleWindow(5)
ONE_SAMPLE = ("--sample-minutes", "0")
# Azure Cloud Shell sets ACC_CLOUD.
CLOUD_SHELL = replace(FACTS, env=EnvFacts("PROD", None, None))
NOT_SIGNED_IN_AZURE = "ERROR: Please run 'az login' to setup account.\n"
NOT_SIGNED_IN_AWS = 'Unable to locate credentials. You can configure credentials by running "aws configure".\n'
REFUSED_GRAPH = (
    "ERROR: (AuthorizationFailed) The client does not have authorization to perform action "
    "'Microsoft.ResourceGraph/resources/read' over scope '/subscriptions/sub-one'.\n"
)
# The report name a run started at 09:30 on 5 January 2026 takes first.
FIRST_NAME = "zoorik-assessment-20260105-0930.pdf"
# An instance with a gp2 data volume of 500 GB, beside the EKS cluster's machines; it is in the disk part.
DISK_INSTANCE = instance(7, "m6i.2xlarge", [("/dev/sdf", "vol-07")])


@dataclass(frozen=True)
class Estate:
    """A made-up estate for the whole command: its scenario, the options, and the files its report folder holds."""

    scenario: Scenario
    options: Tuple[str, ...] = ()
    existing: Tuple[str, ...] = ()

    @property
    def name(self) -> str:
        """Name the estate as its scenario."""
        return self.scenario.name


def azure_signed_in() -> List[Call]:
    """Make az's answers for the signed-in directory, with its one enabled subscription."""
    account = {
        "id": SUB,
        "name": "subscription-one",
        "tenantId": TENANT,
        "state": "Enabled",
        "tenantDisplayName": "Directory One",
        "tenantDefaultDomain": "one.example.test",
    }
    return [az_call(["account", "show"], {"tenantId": TENANT}), az_call(["account", "list"], [account])]


def aws_signed_in() -> List[Call]:
    """Make aws's answers for the signed-in account, with its one enabled region."""
    return [
        aws_call(["sts", "get-caller-identity", "--region", US], {"Account": ACCOUNT}),
        aws_call(["ec2", "describe-regions", "--query", "Regions[].RegionName", "--region", US], [EU]),
    ]


def disk_instance_reads() -> List[Call]:
    """Make the disk part's answers for DISK_INSTANCE after a yes: 100 GB used, so its pool goes on gp3.

    As the self-managed node's: two gp3 volumes of 58 GiB, 9.28 a month against the gp2's 50, a saving of 40.72.
    The region's instances are answered with the EKS cluster's, since both parts take them from one read.
    """
    printed = guest_output("V", [("vol-07", ["/data"])], {"/data": 100}, AWS_KERNEL)
    volumes = {"Volumes": [root_volume(7), volume("vol-07", "gp2", 500, 1500)]}
    return [
        aws_call(["ec2", "describe-volumes", "--region", EU], volumes),
        types_call(),
        ssm_online(["i-07"]),
        *ssm_read("i-07", printed),
        *cloudwatch("vol-07", (6000, 6000, 600e6, 300e6)),
        *ebs_prices(),
    ]


def answered_once(calls: Sequence[Call], shared: Sequence[Call]) -> List[Call]:
    """Give calls with each shared call in place of the calls of its key: a read the run makes once for both parts."""
    keys = {each.key for each in shared}
    return [*(each for each in calls if each.key not in keys), *shared]


def scenario(
    name: str,
    calls: Sequence[Call],
    fetches: Sequence[Fetch] = (),
    answers: Sequence[Optional[bool]] = (),
    installed: Sequence[str] = ("az",),
) -> Scenario:
    """Make a run outside Cloud Shell with the programs installed, answered with calls, fetches and answers."""
    return Scenario(name, FACTS, frozenset(installed), tuple(calls), tuple(fetches), tuple(answers))


def whole_estate(name: str = "whole-estate", options: Tuple[str, ...] = ()) -> Estate:
    """Make both clouds, read over the default window: two clusters and two VMs, a yes for each cloud's VMs.

    Kubernetes: the public AKS cluster's $730 to $438, then $372, then $248, and the EKS cluster's $379 to $277, then
    $204, then $58: $1,109 to $306, a saving of $540 from smaller pods and spare machines (packing included), $139 from
    cheaper machine types and $124 from fewer copies at quiet times. Disks: the Azure VM's $200 to $24 and the
    instance's $50 to $9, so $250 to $33, a saving of $217. Page 1: $1,359 to $339, a saving of $1,020, 75% of the cost
    today. Both parts read the region's VM sizes and instance types once.
    """
    clusters_azure = aks_public(FIVE_MINUTES)
    clusters_aws = eks_node_kinds(FIVE_MINUTES, [DISK_INSTANCE])
    disks_azure = linux_vm()
    reads = [
        *azure_signed_in(),
        *aws_signed_in(),
        *clusters_azure.calls,
        *clusters_aws.calls,
        *disks_azure.calls,
        *disk_instance_reads(),
    ]
    calls = answered_once(reads, [skus(*VM_TYPES, D8S), instance_types(*EC2_TYPES, NITRO_TYPE, XEN_TYPE)])
    fetches = (*clusters_azure.fetches, *disks_azure.fetches)
    run = scenario(name, calls, fetches, (True, True), ("az", "aws", "kubectl"))
    return Estate(run, options)


def clusters_only() -> Estate:
    """Make the public AKS cluster in Azure Cloud Shell, one sample: no VM with data disks, so no Disks pages."""
    clusters = aks_public()
    calls = (*azure_signed_in(), *clusters.calls, *azure_inventory([], []))
    run = replace(scenario("clusters-only", calls, clusters.fetches, installed=("az", "kubectl")), facts=CLOUD_SHELL)
    return Estate(run, ONE_SAMPLE)


def disks_only(name: str = "disks-only") -> Estate:
    """Make the Azure VM with a P30 and a P20, read after a yes, in a subscription with no cluster."""
    disks = linux_vm()
    return Estate(scenario(name, [*azure_signed_in(), aks_list(), *disks.calls], disks.fetches, disks.answers))


def nothing_found() -> Estate:
    """Make a subscription with no cluster and no VM: nothing to report, and nothing failed."""
    return Estate(scenario("nothing-found", [*azure_signed_in(), aks_list(), *azure_inventory([], [])]))


def nothing_readable() -> Estate:
    """Make a subscription whose cluster list and Resource Graph are refused: nothing to report, and both failed."""
    refused = az_failure(graph_args(SUBSCRIPTIONS_QUERY), REFUSED_GRAPH)
    return Estate(scenario("nothing-readable", [*azure_signed_in(), *aks_list_refused().calls, refused]))


def signed_out() -> Estate:
    """Make both CLIs installed and neither signed in."""
    calls = [
        az_failure(["account", "show"], NOT_SIGNED_IN_AZURE),
        aws_failure(["sts", "get-caller-identity", "--region", US], NOT_SIGNED_IN_AWS),
    ]
    return Estate(scenario("signed-out", calls, installed=("az", "aws")))


def no_invoke() -> Estate:
    """Make the public AKS cluster beside a private one under --no-invoke: the private one is named, not read."""
    clusters = aks_public(others=[aks_record("cluster-private", apiServerAccessProfile=PRIVATE)])
    calls = [*azure_signed_in(), *clusters.calls, *azure_inventory([], [])]
    return Estate(
        scenario("no-invoke", calls, clusters.fetches, installed=("az", "kubectl")), ("--no-invoke", *ONE_SAMPLE)
    )


def existing_report() -> Estate:
    """Make the disks-only estate with a report of the same minute in the folder: the new one takes -2."""
    return replace(disks_only("existing-report"), existing=(FIRST_NAME,))


def invoked_cluster() -> Estate:
    """Make a private AKS cluster, read through command invoke over one sample, and no VM."""
    clusters = aks_private()
    calls = [*azure_signed_in(), *clusters.calls, *azure_inventory([], [])]
    return Estate(scenario("invoked-cluster", calls, clusters.fetches), ONE_SAMPLE)


# The estates whose whole run a golden holds.
GOLDEN = (
    whole_estate(),
    clusters_only(),
    disks_only(),
    nothing_found(),
    nothing_readable(),
    Estate(NO_CLI),
    signed_out(),
    no_invoke(),
    whole_estate("anonymize", ("--anonymize",)),
    existing_report(),
)
