"""The Kubernetes part on made-up clusters: each scenario's result, lines and started commands, against goldens."""

import dataclasses
import tempfile
import unittest
from typing import Dict, List

from tests.replay import golden
from tests.replay.fake_cloud import FakeRun
from tests.replay.kubernetes import SCENARIOS, WINDOW, KubernetesScenario
from tests.replay.replay import ReplayHttp, ReplayRunner
from tests.replay.scenarios import FACTS
from zoorik.cloud.aws import AwsCli
from zoorik.cloud.azure import AzureCli
from zoorik.cloud.cli import Host
from zoorik.cloud.started import StartedCommands
from zoorik.kubectl.direct import KUBELOGIN_MISSING, UNREACHABLE, DirectKubeApi
from zoorik.kubectl.invoke import InvokeKubeApi
from zoorik.kubernetes.assessment import ClusterCloud, KubernetesAssessment, aws_clusters, azure_clusters
from zoorik.kubernetes.discovery import INVOKE_OFF, NO_INVOKE, PRIVATE_ENDPOINT, STOPPED_REASON, Readers
from zoorik.kubernetes.model import ClusterResult, KubernetesResult
from zoorik.pricing.machines import AwsMachinePrices, AzureMachinePrices

PART = "Kubernetes"
BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}


@dataclasses.dataclass(frozen=True)
class Outcome:
    """A scenario's run: its result, the document its golden holds, and the answers it never used."""

    result: KubernetesResult
    document: Dict[str, object]
    unused: List[str]


def run(scenario: KubernetesScenario) -> Outcome:
    """Run the Kubernetes part on the scenario's answers."""
    with tempfile.TemporaryDirectory() as temp_root:
        fake = FakeRun(FACTS, temp_root)
        runner = ReplayRunner(scenario.calls, temp_root, fake.clock)
        http = ReplayHttp(scenario.fetches)
        started = StartedCommands()
        host = Host(runner, fake.clock, temp_root, False)
        azure, aws = AzureCli(host, started), AwsCli(host, started)
        readers = Readers(lambda config: DirectKubeApi(host, config), lambda cluster: InvokeKubeApi(azure, cluster))
        clouds: List[ClusterCloud] = []
        if scenario.azure is not None:
            clouds.append(azure_clusters(azure, scenario.azure, AzureMachinePrices(http), readers, scenario.no_invoke))
        if scenario.aws is not None:
            clouds.append(aws_clusters(aws, scenario.aws, AwsMachinePrices(aws, fake.clock), readers))
        result = KubernetesAssessment(clouds, WINDOW, fake.console.part(PART)).run()
    document: Dict[str, object] = {
        "console": fake.console.lines.get(PART, []),
        "started": started.note(),
        "result": {**dataclasses.asdict(result), "today": result.today, "after": result.after, "saving": result.saving},
    }
    return Outcome(result, document, [*runner.unused(), *http.unused()])


def result_of(name: str) -> KubernetesResult:
    """Give the result of the scenario called name."""
    return run(BY_NAME[name]).result


def only_cluster(name: str) -> ClusterResult:
    """Give the one cluster the scenario called name assesses."""
    clusters = result_of(name).clusters
    if len(clusters) != 1:
        raise AssertionError(f"{name} assesses {len(clusters)} clusters")
    return clusters[0]


class KubernetesGoldenTest(unittest.TestCase):
    """Each scenario's run matches its golden, and takes every answer it was given."""

    def test_each_scenario_matches_its_golden(self) -> None:
        """The result, the lines and the started commands are each scenario's golden."""
        for scenario in SCENARIOS:
            with self.subTest(scenario.name):
                outcome = run(scenario)
                self.assertEqual(outcome.unused, [])
                golden.check(f"kubernetes_{scenario.name.replace('-', '_')}", outcome.document)

    def test_no_scenario_makes_a_kubectl_config_call(self) -> None:
        """A run makes only the calls its scenario answers, and all of them; none is a kubectl config call."""
        for scenario in SCENARIOS:
            with self.subTest(scenario.name):
                self.assertEqual(run(scenario).unused, [])
                kubectl = [call.argv for call in scenario.calls if call.argv[0] == "kubectl"]
                self.assertEqual([argv for argv in kubectl if "config" in argv], [])


class WorkedFigureTest(unittest.TestCase):
    """The figure the plan works out by hand, on a cluster read with kubectl."""

    def test_a_public_cluster_goes_from_730_to_438_split_134_and_158(self) -> None:
        """Step 1 saves $0 and step 2 $292; A's share is $134 and B's $158, and the System pool keeps three."""
        cluster = only_cluster("aks-public")
        figures = [(step.name, step.before, step.after, step.saving) for step in cluster.steps]
        self.assertEqual(figures, [("Smaller pods", 730, 730, 0), ("Spare machines", 730, 438, 292)])
        self.assertEqual([(line.today, line.after) for line in cluster.lines], [(5, 3)])
        self.assertEqual([(each.name, each.share) for each in cluster.workloads], [("a", 134), ("b", 158)])


class ReadMethodTest(unittest.TestCase):
    """How a cluster is read follows from its settings alone."""

    def test_a_private_or_ip_limited_cluster_is_read_through_command_invoke(self) -> None:
        """Both clusters are read, and the run says it started command invoke in each."""
        for name in ("aks-private", "aks-authorized-ranges"):
            with self.subTest(name):
                outcome = run(BY_NAME[name])
                started = str(outcome.document["started"])
                self.assertEqual(len(outcome.result.clusters), 1)
                self.assertIn("az aks command invoke had already started in 1 cluster", started)

    def test_each_aks_cluster_not_read_is_named_with_why(self) -> None:
        """Stopped, command invoke off, --no-invoke and Entra ID without kubelogin each name the cluster, unread."""
        cases = {
            "aks-stopped": {"cluster-stopped": STOPPED_REASON},
            "aks-run-command-off": {"cluster-private": INVOKE_OFF},
            "aks-no-invoke": {"cluster-private": NO_INVOKE, "cluster-ranges": NO_INVOKE},
            "aks-entra-without-kubelogin": {"cluster-entra": KUBELOGIN_MISSING},
        }
        for name, reasons in cases.items():
            with self.subTest(name):
                result = result_of(name)
                self.assertEqual({named.name: named.reason for named in result.not_assessed}, reasons)
                self.assertEqual(result.clusters, ())

    def test_each_eks_cluster_not_read_is_named_with_why(self) -> None:
        """A status other than ACTIVE, node groups that can't be read and an API server out of reach are named."""
        result = result_of("eks-named")
        reasons = {named.name: named.reason for named in result.not_assessed}
        self.assertEqual(
            reasons,
            {
                "cluster-creating": "the cluster's status is CREATING",
                "cluster-groups": "could not read its node groups: access denied",
                "cluster-private": PRIVATE_ENDPOINT,
                "cluster-public": UNREACHABLE,
            },
        )
        self.assertEqual([(scope.name, scope.reason) for scope in result.unread], [("us-east-1", "access denied")])


class NodeKindsTest(unittest.TestCase):
    """Each kind of EKS node is its own pool, priced as it is bought."""

    def test_each_kind_is_a_pool_and_fargate_is_not_a_machine(self) -> None:
        """Spot lines take the zones' average spot price; each node group drops a machine, the others keep theirs."""
        cluster = only_cluster("eks-node-kinds")
        lines = [
            (line.kind, line.pool, line.machine_type, line.capacity, line.hourly, line.today, line.after)
            for line in cluster.lines
        ]
        self.assertEqual(
            lines,
            [
                ("Auto Scaling group", "workers-self", "m6i.large", "spot", 0.04, 1, 1),
                ("NodePool", "burst", "m6i.xlarge", "regular", 0.2, 1, 1),
                ("node group", "apps-on-demand", "m6i.large", "regular", 0.1, 2, 1),
                ("node group", "apps-spot", "m6i.large", "spot", 0.04, 2, 1),
            ],
        )
        self.assertEqual((cluster.today, cluster.after, cluster.not_machines), (379, 277, 1))
        shares = {each.name: each.share for each in cluster.workloads}
        self.assertEqual(shares, {"report": 0, "tools": 0, "web": 62, "worker": 40})
