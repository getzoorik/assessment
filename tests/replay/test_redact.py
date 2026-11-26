"""Redaction while recording: kubectl JSON only through the package's allowlist, and of a kubeconfig only one fact."""

import json
import unittest
from typing import List

from tests.replay.clusters import SMALL, SPREAD, container, node, pod
from tests.replay.redact import Redactor, UnredactedOutput, kubeconfig, pod_page
from zoorik.kubectl.objects import Json

KUBECONFIG = """apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: c2VjcmV0LWNh
    server: https://cluster-one.example.test:443
  name: cluster-one
users:
- name: someone
  user:
    exec:
      command: kubelogin
      args: [get-token, --login, azurecli]
"""
PODS_PATH = "/api/v1/pods?limit=500"


def raw_page(kind: str, items: List[Json], token: str = "") -> str:
    """Write a page of a list as the API server gives it."""
    return json.dumps(
        {"kind": kind, "apiVersion": "v1", "metadata": {"resourceVersion": "4096", "continue": token}, "items": items}
    )


def recorded_pod_page(items: List[Json], token: str = "") -> str:
    """Give a page of pods as a recording keeps it while the run goes on."""
    return Redactor().output(["kubectl", "get", "--raw", PODS_PATH], raw_page("PodList", items, token))


class RedactorTest(unittest.TestCase):
    """Redactor.output and Redactor.written."""

    def test_a_page_keeps_only_its_kinds_allowlist_and_its_token(self) -> None:
        """A page of nodes keeps each node's allowed fields and the next page's token, and nothing else."""
        page = raw_page("NodeList", [node("node-one", "westeurope-1")], token="page1=")
        output = json.loads(Redactor().output(["kubectl", "get", "--raw", "/api/v1/nodes?limit=500"], page))
        self.assertEqual(output["metadata"], {"continue": "page1="})
        status = output["items"][0]["status"]
        self.assertEqual(sorted(status), ["allocatable", "capacity", "nodeInfo"])
        self.assertEqual(status["nodeInfo"], {"kubeletVersion": "v1.30.4"})
        for private in ("10.224.0.4", "machine-private", "registry.example.test", "uid-node-one", "podCIDR", "4096"):
            self.assertNotIn(private, json.dumps(output))

    def test_a_pod_keeps_no_env_image_command_or_annotation(self) -> None:
        """Pods keep no env value, image, command or annotation, and the pods that ended are not kept."""
        output = recorded_pod_page(list(SMALL.objects["pods"]))
        for private in ("private-value", "private-flag", "registry.example.test", "private-note", "10.244.0.12"):
            self.assertNotIn(private, output)
        names = [item["metadata"]["name"] for item in json.loads(output)["items"]]
        self.assertEqual(len(names), len(SMALL.objects["pods"]) - 2)
        self.assertNotIn("report-28099000-q9w8e", names)

    def test_the_version_check_keeps_only_the_version(self) -> None:
        """Of the /version answer, only gitVersion is kept."""
        answer = json.dumps({"major": "1", "minor": "30", "gitVersion": "v1.30.4", "platform": "linux/amd64"})
        output = Redactor().output(["kubectl", "get", "--raw", "/version"], answer)
        self.assertEqual(json.loads(output), {"gitVersion": "v1.30.4"})

    def test_other_output_is_kept_whole(self) -> None:
        """Output that is not kubectl JSON, such as az JSON or a kubectl top table, is kept as it is."""
        redactor = Redactor()
        self.assertEqual(redactor.output(["az", "account", "show"], '{"name": "one"}'), '{"name": "one"}')
        top = "shop   web-6d4b75cb6d-abcde   app   120m   200Mi\n"
        self.assertEqual(redactor.output(["kubectl", "top", "pods", "-A", "--containers", "--no-headers"], top), top)

    def test_kubectl_json_of_a_kind_with_no_allowlist_is_refused(self) -> None:
        """Without an allowlist for its kind, a kubectl answer stops the recording."""
        with self.assertRaisesRegex(UnredactedOutput, "kind SecretList"):
            Redactor().output(["kubectl", "get", "--raw", "/api/v1/secrets"], '{"kind": "SecretList", "items": []}')

    def test_a_kubeconfig_keeps_only_whether_it_needs_kubelogin(self) -> None:
        """A written kubeconfig is recorded as a stand-in that tells only whether its user signs in with kubelogin."""
        stand_in = Redactor().written(KUBECONFIG)
        self.assertEqual(stand_in, "# kubeconfig redacted while recording; its user signs in through kubelogin\n")
        for private in ("c2VjcmV0LWNh", "example.test", "cluster-one", "someone"):
            self.assertNotIn(private, stand_in)
        without = kubeconfig(KUBECONFIG.replace("kubelogin", "aws"))
        self.assertEqual(without, "# kubeconfig redacted while recording\n")


class NarrowTest(unittest.TestCase):
    """Redactor.narrow_pod_pages and pod_page."""

    def test_a_clusters_pod_pages_are_narrowed_together(self) -> None:
        """A label that a spread constraint on one page selects is kept on the other page; other labels go."""
        first = recorded_pod_page(
            [pod("shop", "web-one", "node-one", [container("app")], labels={"app": "web", "team": "one"})], "page1="
        )
        spread = {"topologySpreadConstraints": [SPREAD]}
        second = recorded_pod_page(
            [
                pod(
                    "shop",
                    "web-two",
                    "node-one",
                    [container("app")],
                    labels={"app": "web", "tier": "front"},
                    spec=spread,
                )
            ]
        )
        self.assertEqual(json.loads(first)["items"][0]["metadata"]["labels"], {"app": "web", "team": "one"})
        narrowed = [json.loads(page) for page in Redactor().narrow_pod_pages([first, second])]
        self.assertEqual(
            [page["items"][0]["metadata"]["labels"] for page in narrowed], [{"app": "web"}, {"app": "web"}]
        )
        self.assertEqual(narrowed[0]["metadata"], {"continue": "page1="})
        self.assertEqual([page["kind"] for page in narrowed], ["PodList", "PodList"])

    def test_pod_pages_are_told_apart(self) -> None:
        """Only a page of pods is a pod page; a page of nodes and a top table are not."""
        self.assertTrue(pod_page(recorded_pod_page([])))
        self.assertFalse(pod_page('{"items": [], "kind": "NodeList"}'))
        self.assertFalse(pod_page("shop   web-6d4b75cb6d-abcde   app   120m   200Mi\n"))
