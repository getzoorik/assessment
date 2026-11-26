"""Redaction while recording."""

import json
import unittest

from tests.replay.redact import Keep, Redactor, UnredactedOutput, kubeconfig, prune

NODE_LIST: Keep = {"kind": True, "items": {"metadata": {"name": True}, "status": {"capacity": True}}}
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


class PruneTest(unittest.TestCase):
    """prune."""

    def test_a_field_off_the_allowlist_is_dropped(self) -> None:
        """Only the allowed keys stay, in every item of a list."""
        nodes = {
            "kind": "NodeList",
            "metadata": {"resourceVersion": "12"},
            "items": [
                {
                    "metadata": {"name": "node-one", "annotations": {"owner": "someone@example.test"}},
                    "spec": {"providerID": "azure:///one"},
                    "status": {"capacity": {"cpu": "4"}, "addresses": [{"address": "10.0.0.4"}]},
                }
            ],
        }
        self.assertEqual(
            prune(nodes, NODE_LIST),
            {"kind": "NodeList", "items": [{"metadata": {"name": "node-one"}, "status": {"capacity": {"cpu": "4"}}}]},
        )

    def test_true_keeps_a_value_whole(self) -> None:
        """A rule of True keeps everything under its key."""
        self.assertEqual(
            prune({"status": {"capacity": {"cpu": "4"}}}, {"status": True}), {"status": {"capacity": {"cpu": "4"}}}
        )

    def test_a_value_of_another_shape_becomes_null(self) -> None:
        """A value where the rule expects an object is dropped to null, so nothing unexpected is kept."""
        self.assertEqual(prune({"metadata": "node-one"}, {"metadata": {"name": True}}), {"metadata": None})


class RedactorTest(unittest.TestCase):
    """Redactor."""

    def test_kubectl_json_is_pruned_by_its_kind(self) -> None:
        """A kubectl answer keeps only its kind's allowlist."""
        answer = json.dumps({"kind": "NodeList", "items": [{"metadata": {"name": "node-one", "uid": "u-1"}}]})
        output = Redactor({"NodeList": NODE_LIST}).output(["kubectl", "get", "--raw", "/api/v1/nodes"], answer)
        self.assertEqual(json.loads(output), {"kind": "NodeList", "items": [{"metadata": {"name": "node-one"}}]})

    def test_other_output_is_kept_whole(self) -> None:
        """Output that is not kubectl JSON, such as az JSON or a kubectl top table, is kept as it is."""
        redactor = Redactor({})
        self.assertEqual(redactor.output(["az", "account", "show"], '{"name": "one"}'), '{"name": "one"}')
        top = "NAME       CPU(cores)   MEMORY(bytes)\nnode-one   120m         900Mi\n"
        self.assertEqual(redactor.output(["kubectl", "top", "nodes"], top), top)

    def test_kubectl_json_of_a_kind_with_no_allowlist_is_refused(self) -> None:
        """Without an allowlist for its kind, a kubectl answer stops the recording."""
        with self.assertRaisesRegex(UnredactedOutput, "kind PodList"):
            Redactor({"NodeList": NODE_LIST}).output(["kubectl", "get", "pods"], '{"kind": "PodList", "items": []}')

    def test_a_kubeconfig_keeps_only_whether_it_needs_kubelogin(self) -> None:
        """A written kubeconfig is recorded as a stand-in that tells only whether its user signs in with kubelogin."""
        stand_in = Redactor({}).written(KUBECONFIG)
        self.assertEqual(stand_in, "# kubeconfig redacted while recording; its user signs in through kubelogin\n")
        for private in ("c2VjcmV0LWNh", "example.test", "cluster-one", "someone"):
            self.assertNotIn(private, stand_in)
        without = kubeconfig(KUBECONFIG.replace("kubelogin", "aws"))
        self.assertEqual(without, "# kubeconfig redacted while recording\n")
