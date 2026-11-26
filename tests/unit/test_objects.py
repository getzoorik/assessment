"""The allowlist: what is kept of each kind, the jq filter generated from it, and the typed objects read from it."""

import unittest
from fractions import Fraction
from typing import Dict, List

from tests.replay.clusters import (
    ANTI_AFFINITY,
    DEFAULT_TOLERATIONS,
    LINUX_ONLY,
    SMALL,
    SPREAD,
    SPREAD_PREFERRED,
    WEB_LABELS,
    autoscaler,
    budget,
    container,
    node,
    owner,
    pod,
    volume,
)
from zoorik.cloud.fields import Record
from zoorik.errors import UnexpectedResponse
from zoorik.kubectl.api import Scaling, Snapshot, snapshot
from zoorik.kubectl.objects import (
    ADDON_MODE,
    AUTOSCALER,
    BUDGET,
    JQ_HELPERS,
    KINDS,
    NODE,
    POD,
    PODS,
    REQUIRED,
    SCALING_KINDS,
    VOLUME,
    WHOLE,
    AffinityTerm,
    Autoscaler,
    Container,
    CsiNode,
    DisruptionBudget,
    Each,
    Fields,
    FoldedKeys,
    HostPort,
    InitContainer,
    Json,
    LabelSelector,
    LimitItem,
    LimitRange,
    Node,
    NodeSelectorTerm,
    Owned,
    OwnerRef,
    Page,
    PersistentVolume,
    PersistentVolumeClaim,
    Pod,
    Requirement,
    SpreadConstraint,
    Taint,
    Toleration,
    jq,
    jq_filter,
    label_keys,
    read_budget,
    read_volume,
    slim,
    slim_page,
)
from zoorik.kubectl.usage import Usage

MEBIBYTE = 2**20
GIBIBYTE = 2**30
NO_USAGE = Usage({}, None)


def page(kind_name: str, items: List[Json], token: str = "") -> Json:
    """Make a page of a list as the API server gives it."""
    return {
        "kind": kind_name,
        "apiVersion": "v1",
        "metadata": {"resourceVersion": "4096", "continue": token},
        "items": items,
    }


def read_small() -> Snapshot:
    """Read the small cluster's objects as the direct read keeps them, each kind's list in one page."""
    kept = {
        kind.name: slim_page(page(kind.list_kind, list(SMALL.objects[kind.name])), kind).items
        for kind in (*KINDS, *SCALING_KINDS)
    }
    return snapshot({kind.name: kept[kind.name] for kind in KINDS}, kept, NO_USAGE)


def pods_by_name(read: Snapshot) -> Dict[str, Pod]:
    """Give the snapshot's pods by name."""
    return {item.name: item for item in read.pods}


class SlimTest(unittest.TestCase):
    """slim: only the allowed fields."""

    def test_a_node_keeps_only_its_allowed_fields(self) -> None:
        """A node keeps its name, labels, taints, provider id, capacity, allocatable and kubelet version."""
        raw = node("node-one", "westeurope-1")
        self.assertEqual(
            slim(raw, NODE, "nodes[0]"),
            {
                "metadata": {"name": "node-one", "labels": raw["metadata"]["labels"]},
                "spec": {"providerID": raw["spec"]["providerID"]},
                "status": {
                    "capacity": raw["status"]["capacity"],
                    "allocatable": raw["status"]["allocatable"],
                    "nodeInfo": {"kubeletVersion": "v1.30.4"},
                },
            },
        )

    def test_a_pod_keeps_only_its_allowed_fields(self) -> None:
        """A pod keeps no image, command, env, annotation or preferred term, and only the controller owner."""
        raw = SMALL.objects["pods"][0]
        self.assertEqual(
            slim(raw, POD, "pods[0]"),
            {
                "metadata": {
                    "name": "web-6d4b75cb6d-abcde",
                    "namespace": "shop",
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "web-6d4b75cb6d", "controller": True}],
                    "labels": WEB_LABELS,
                },
                "spec": {
                    "nodeName": "node-one",
                    "containers": [
                        {
                            "name": "app",
                            "resources": {
                                "requests": {"cpu": "250m", "memory": "256Mi"},
                                "limits": {"cpu": "500m", "memory": "512Mi"},
                            },
                        },
                        {"name": "proxy", "resources": {"limits": {"cpu": "100m", "memory": "64Mi"}}},
                    ],
                    "affinity": {
                        "nodeAffinity": {REQUIRED: {"nodeSelectorTerms": [LINUX_ONLY]}},
                        "podAffinity": {},
                        "podAntiAffinity": {REQUIRED: [ANTI_AFFINITY]},
                    },
                    "tolerations": DEFAULT_TOLERATIONS,
                    "topologySpreadConstraints": [SPREAD, SPREAD_PREFERRED],
                    "volumes": [],
                },
                "status": {"phase": "Running"},
            },
        )

    def test_an_autoscaler_and_a_budget_keep_only_their_allowed_fields(self) -> None:
        """An autoscaler keeps its namespace and target's kind and name; a budget its namespace, selector and floor."""
        self.assertEqual(
            slim(autoscaler("shop", "web", "Rollout"), AUTOSCALER, "horizontalpodautoscalers[0]"),
            {"metadata": {"namespace": "shop"}, "spec": {"scaleTargetRef": {"kind": "Rollout", "name": "web"}}},
        )
        selector = {"matchLabels": {"tier": "front"}}
        self.assertEqual(
            slim(budget("shop", "web", selector, minAvailable="50%", maxUnavailable=1), BUDGET, "budgets[0]"),
            {"metadata": {"namespace": "shop"}, "spec": {"selector": selector, "minAvailable": "50%"}},
        )

    def test_a_list_keeps_only_the_items_whose_where_field_is_set(self) -> None:
        """With where, an item is kept when that field is there and not false, as jq's select keeps it."""
        rule = Each(Fields({"name": WHOLE}), where="controller")
        items = [{"name": "one", "controller": True}, {"name": "two", "controller": False}, {"name": "three"}]
        self.assertEqual(slim(items, rule, "owners"), [{"name": "one"}])

    def test_folded_keys_are_kept_in_any_case(self) -> None:
        """The tier keys are kept whatever their case, and every other attribute goes."""
        attributes = {"skuName": "Premium_LRS", "StorageAccountType": "Premium_LRS", "cachingMode": "ReadOnly"}
        self.assertEqual(
            slim(attributes, FoldedKeys(("skuname", "storageaccounttype")), "attributes"),
            {"skuName": "Premium_LRS", "StorageAccountType": "Premium_LRS"},
        )

    def test_a_value_of_another_shape_is_unexpected(self) -> None:
        """An object where a list belongs, or the other way round, is refused and named by its path."""
        with self.assertRaisesRegex(UnexpectedResponse, r"^unexpected answer: pods\[0\]\.metadata is not an object$"):
            slim({"metadata": "web"}, POD, "pods[0]")
        with self.assertRaisesRegex(
            UnexpectedResponse, r"^unexpected answer: pods\[0\]\.spec\.containers is not a list$"
        ):
            slim({"spec": {"containers": {}}}, POD, "pods[0]")


class SlimPageTest(unittest.TestCase):
    """slim_page: one page of a list."""

    def test_a_page_keeps_its_items_and_its_next_token(self) -> None:
        """The items are kept by the kind's rule, with the next page's token; the last page has none."""
        nodes = [node("node-one", "westeurope-1")]
        self.assertEqual(slim_page(page("NodeList", nodes, "page1="), KINDS[0]).token, "page1=")
        self.assertEqual(slim_page(page("NodeList", nodes), KINDS[0]), Page([slim(nodes[0], NODE, "nodes[0]")], None))
        self.assertIsNone(slim_page({"kind": "NodeList", "items": []}, KINDS[0]).token)

    def test_a_page_of_another_kind_is_unexpected(self) -> None:
        """An answer of another kind than the list asked for is refused."""
        with self.assertRaisesRegex(UnexpectedResponse, r"^unexpected answer: kubectl gave a Status for pods$"):
            slim_page({"kind": "Status", "items": []}, PODS)

    def test_ended_pods_and_objects_with_no_controller_are_not_kept(self) -> None:
        """Pods that ended, and ReplicaSets and Jobs with no controller owner, are left out."""
        kept = {
            kind.name: len(slim_page(page(kind.list_kind, list(SMALL.objects[kind.name])), kind).items)
            for kind in (*KINDS, *SCALING_KINDS)
        }
        self.assertEqual(
            kept,
            {
                "nodes": 2,
                "pods": 8,
                "replicasets": 1,
                "jobs": 1,
                "persistentvolumeclaims": 2,
                "persistentvolumes": 4,
                "csinodes": 2,
                "limitranges": 1,
                "horizontalpodautoscalers": 1,
                "poddisruptionbudgets": 2,
            },
        )


class JqTest(unittest.TestCase):
    """jq and jq_filter: the same rules, written for jq."""

    def test_each_rule_is_written_as_jq(self) -> None:
        """Fields, Each with and without where, FoldedKeys and Whole each have their jq form."""
        rule = Fields(
            {"a": WHOLE, "b": Each(Fields({"c": WHOLE}), where="c"), "d": Each(Fields({})), "e": FoldedKeys(("x",))}
        )
        self.assertEqual(
            jq(rule),
            '(k("a"; .) + k("b"; e("c"; (k("c"; .)))) + k("d"; map({})) '
            '+ k("e"; with_entries(select(.key | ascii_downcase | . == "x"))))',
        )

    def test_each_kinds_filter_keeps_its_items_and_applies_its_test(self) -> None:
        """A kind's filter starts with the helpers, maps every item by its rule, then selects by its test."""
        nodes = jq_filter(KINDS[0])
        self.assertEqual(nodes, f"{JQ_HELPERS} [.items[] | {jq(NODE)}]")
        self.assertIn('| select(.status.phase != "Succeeded" and .status.phase != "Failed")]', jq_filter(PODS))
        self.assertIn("| select((.metadata.ownerReferences // []) | length > 0)]", jq_filter(KINDS[2]))

    def test_the_pods_filter_narrows_their_labels(self) -> None:
        """The pods' filter names the keys placement and the budgets in $b use, then keeps only those labels."""
        pods = jq_filter(PODS)
        self.assertIn("+ [$b[][] | (.spec.selector // empty) | sk[]] ", pods)
        self.assertIn(f'+ ["{ADDON_MODE}"] | map({{(.): true}}) | add) as $keep', pods)
        self.assertTrue(pods.endswith("with_entries(select($keep[.key])) else . end]"))

    def test_no_filter_holds_a_single_quote(self) -> None:
        """Each filter goes inside single quotes in the shell script, so none holds one."""
        self.assertEqual([kind.name for kind in (*KINDS, *SCALING_KINDS) if "'" in jq_filter(kind)], [])


class ReadTest(unittest.TestCase):
    """The typed objects read from what was kept."""

    def setUp(self) -> None:
        """Read the small cluster."""
        self.read = read_small()
        self.pods = pods_by_name(self.read)

    def test_a_node_is_read_in_scheduler_units(self) -> None:
        """Capacity and allocatable are read in millicores and bytes, with the node's taints and provider id."""
        self.assertEqual(
            self.read.nodes[1],
            Node(
                name="node-two",
                labels=SMALL.objects["nodes"][1]["metadata"]["labels"],
                taints=(Taint("dedicated", "db", "NoSchedule"),),
                provider_id="azure:///node-two",
                capacity={"cpu": 4000, "memory": 16393364 * 1024, "pods": 110, "ephemeral-storage": 129886128 * 1024},
                allocatable={"cpu": 3860, "memory": 12880532 * 1024, "pods": 110, "ephemeral-storage": 119703055367},
                kubelet_version="v1.30.4",
            ),
        )

    def test_requests_are_read_as_stored_with_no_limit_fallback(self) -> None:
        """A container that sets only limits has no requests; the limits are never read as requests."""
        self.assertEqual(
            self.pods["web-6d4b75cb6d-abcde"].containers,
            (
                Container("app", {"cpu": 250, "memory": 256 * MEBIBYTE}, {"cpu": 500, "memory": 512 * MEBIBYTE}, ()),
                Container("proxy", {}, {"cpu": 100, "memory": 64 * MEBIBYTE}, ()),
            ),
        )

    def test_only_the_controller_owner_is_read(self) -> None:
        """A pod's controller is its controller owner reference, never another owner; with none, it has none."""
        two_owners = pod(
            "shop",
            "batch-one",
            "node-one",
            [container("app")],
            owners=[owner("Workflow", "flow-one", controller=False), owner("ReplicaSet", "batch-7c9d8b")],
        )
        kept = slim_page(page("PodList", [two_owners]), PODS).items
        read = snapshot(
            {**{kind.name: [] for kind in KINDS}, "pods": kept}, {kind.name: [] for kind in SCALING_KINDS}, NO_USAGE
        )
        self.assertEqual(read.pods[0].controller, OwnerRef("ReplicaSet", "batch-7c9d8b"))
        self.assertEqual(self.pods["web-6d4b75cb6d-abcde"].controller, OwnerRef("ReplicaSet", "web-6d4b75cb6d"))
        self.assertIsNone(self.pods["step-one"].controller)
        self.assertIsNone(self.pods["debug"].controller)

    def test_a_pod_owned_by_a_node_is_static(self) -> None:
        """Only the pod whose controller is a Node is static."""
        self.assertEqual([item.name for item in self.read.pods if item.static], ["kube-proxy-node-two"])

    def test_scheduling_terms_are_read(self) -> None:
        """Required node affinity, required anti-affinity, spread constraints and tolerations are read as typed."""
        web = self.pods["web-6d4b75cb6d-abcde"]
        linux = Requirement("kubernetes.io/os", "In", ("linux",))
        self.assertEqual(web.node_affinity, (NodeSelectorTerm((linux,), ()),))
        self.assertEqual(
            web.anti_affinity,
            (
                AffinityTerm(
                    LabelSelector({}, (Requirement("app", "In", ("web",)),)),
                    (),
                    None,
                    "kubernetes.io/hostname",
                    ("version",),
                    (),
                ),
            ),
        )
        self.assertEqual(web.affinity, ())
        self.assertEqual(
            web.spread,
            (
                SpreadConstraint(
                    1,
                    "topology.kubernetes.io/zone",
                    "DoNotSchedule",
                    LabelSelector({"app": "web"}, ()),
                    None,
                    None,
                    None,
                    ("pod-template-hash",),
                ),
                SpreadConstraint(
                    1,
                    "kubernetes.io/hostname",
                    "ScheduleAnyway",
                    LabelSelector({"tier": "front"}, ()),
                    None,
                    None,
                    None,
                    (),
                ),
            ),
        )
        self.assertEqual(
            web.tolerations,
            (
                Toleration("node.kubernetes.io/not-ready", "Exists", None, "NoExecute"),
                Toleration("node.kubernetes.io/unreachable", "Exists", None, "NoExecute"),
            ),
        )
        db = self.pods["db-0"]
        self.assertEqual(db.affinity[0].mismatch_label_keys, ("track",))
        self.assertEqual(db.node_selector, {"kubernetes.io/os": "linux"})
        self.assertEqual(db.tolerations, (Toleration("dedicated", "Equal", "db", "NoSchedule"),))
        self.assertIsNone(web.requests)

    def test_pod_resources_ports_inits_and_claims_are_read(self) -> None:
        """Pod-level resources, overhead, host ports, init containers with sidecars, and claim names are read."""
        db = self.pods["db-0"]
        self.assertEqual((db.requests, db.limits), ({"cpu": 1000, "memory": 3 * GIBIBYTE}, {"memory": 4 * GIBIBYTE}))
        self.assertEqual(db.claims, ("data-db-0",))
        report = self.pods["report-28100000-x7k2p"]
        self.assertEqual(report.overhead, {"cpu": 250, "memory": 120 * MEBIBYTE})
        self.assertEqual(
            report.init_containers,
            (
                InitContainer("setup", {"cpu": 200}, {}, sidecar=False),
                InitContainer("log-shipper", {"cpu": 50, "memory": 32 * MEBIBYTE}, {}, sidecar=True),
            ),
        )
        self.assertEqual(self.pods["agent-x2k4p"].containers[0].host_ports, (HostPort(9100, "TCP"),))
        pending = self.pods["web-6d4b75cb6d-fghij"]
        self.assertIsNone(pending.node)

    def test_owners_claims_volumes_csi_nodes_and_limit_ranges_are_read(self) -> None:
        """The other kinds are read as typed, each with what a later step needs."""
        self.assertEqual(
            self.read.owners,
            (
                Owned("ReplicaSet", "shop", "web-6d4b75cb6d", OwnerRef("Deployment", "web")),
                Owned("Job", "batch", "report-28100000", OwnerRef("CronJob", "report")),
            ),
        )
        self.assertEqual(
            self.read.claims,
            (
                PersistentVolumeClaim("shop", "data-db-0", "pv-data-db-0"),
                PersistentVolumeClaim("shop", "waiting", None),
            ),
        )
        zone = Requirement("topology.disk.csi.azure.com/zone", "In", ("westeurope-2",))
        self.assertEqual(
            self.read.volumes,
            (
                PersistentVolume(
                    "pv-data-db-0", "disk.csi.azure.com", "Premium_LRS", (NodeSelectorTerm((zone,), ()),), False
                ),
                PersistentVolume("pv-cache", "disk.csi.azure.com", "StandardSSD_LRS", None, False),
                PersistentVolume("pv-legacy", "disk.csi.azure.com", None, None, True),
                PersistentVolume("pv-share", None, None, None, False),
            ),
        )
        self.assertEqual(
            self.read.csi_nodes,
            (
                CsiNode("node-one", {"disk.csi.azure.com": 8, "file.csi.azure.com": None}),
                CsiNode("node-two", {"disk.csi.azure.com": 8}),
            ),
        )
        self.assertEqual(
            self.read.limit_ranges,
            (
                LimitRange(
                    "shop",
                    (
                        LimitItem(
                            "Container",
                            {"cpu": 50, "memory": 64 * MEBIBYTE},
                            {"cpu": 2000, "memory": 4 * GIBIBYTE},
                            {"cpu": Fraction(4)},
                        ),
                        LimitItem("PersistentVolumeClaim", {"storage": GIBIBYTE}, {}, {}),
                    ),
                ),
            ),
        )

    def test_autoscalers_and_budgets_are_read(self) -> None:
        """An autoscaler names its target; a budget its selector and a whole-number floor, else none."""
        self.assertEqual(
            self.read.scaling,
            Scaling(
                (Autoscaler("shop", "Deployment", "web"),),
                (
                    DisruptionBudget("shop", LabelSelector({"tier": "front"}, ()), 1),
                    DisruptionBudget("shop", LabelSelector({"role": "db"}, ()), None),
                ),
                None,
            ),
        )

        def read(raw: Json) -> DisruptionBudget:
            """Read a budget as slim keeps it."""
            return read_budget(Record(slim(raw, BUDGET, "budgets[0]"), "budgets[0]"))

        self.assertEqual(read(budget("shop", "web", None, minAvailable="50%")), DisruptionBudget("shop", None, None))
        self.assertEqual(
            read(budget("shop", "web", {}, minAvailable=3)), DisruptionBudget("shop", LabelSelector({}, ()), 3)
        )
        with self.assertRaisesRegex(UnexpectedResponse, r"minAvailable is not a whole number or a text$"):
            read(budget("shop", "web", {}, minAvailable=1.5))

    def test_two_tier_keys_name_one_tier_only_when_they_agree(self) -> None:
        """The two tier keys with one value give it; with two values, the volume names no tier."""

        def tier(attributes: Dict[str, str]) -> object:
            """Read the tier of a CSI volume with these attributes."""
            raw = volume("pv-one", {"csi": {"driver": "disk.csi.azure.com", "volumeAttributes": attributes}})
            return read_volume(Record(slim(raw, VOLUME, "pv-one"), "pv-one")).tier

        self.assertEqual(tier({"skuname": "Premium_LRS", "storageAccountType": "Premium_LRS"}), "Premium_LRS")
        self.assertIsNone(tier({"skuname": "Premium_LRS", "storageAccountType": "Standard_LRS"}))
        self.assertIsNone(tier({"cachingMode": "ReadOnly"}))


class LabelsTest(unittest.TestCase):
    """The pod labels the run reads."""

    def test_the_keys_come_from_required_placement_terms_and_budgets(self) -> None:
        """DoNotSchedule spread, required affinity terms and budgets' selectors name the keys; preferences name none."""
        read = read_small()
        self.assertEqual(
            label_keys(read.pods, ()), frozenset({"app", "pod-template-hash", "version", "role", "track", ADDON_MODE})
        )
        self.assertEqual(
            label_keys(read.pods, read.scaling.budgets),
            frozenset({"app", "pod-template-hash", "version", "role", "track", "tier", ADDON_MODE}),
        )

    def test_a_label_no_required_placement_term_or_budget_uses_is_dropped(self) -> None:
        """Each pod keeps only the labels whose keys a required term or a budget uses, and the add-on manager's mode."""
        labels = {item.name: item.labels for item in read_small().pods}
        web = {"app": "web", "pod-template-hash": "6d4b75cb6d", "version": "v2", "tier": "front"}
        self.assertEqual(
            labels,
            {
                "web-6d4b75cb6d-abcde": web,
                "web-6d4b75cb6d-fghij": web,
                "agent-x2k4p": {"app": "agent"},
                "kube-proxy-node-two": {ADDON_MODE: "Reconcile"},
                "report-28100000-x7k2p": {"app": "report-28100000-x7k2p"},
                "db-0": {"app": "db-0"},
                "debug": {"app": "debug", "role": "cache", "track": "stable"},
                "step-one": {"app": "step-one"},
            },
        )
