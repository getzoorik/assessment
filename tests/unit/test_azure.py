"""Azure through az."""

import json
import os
from datetime import datetime, timezone
from typing import Dict, List

from tests.replay.recording import Call
from tests.unit.clis import CloudTestCase, answer, az, failing
from zoorik.cloud.azure import (
    CLUSTERS_TIMEOUT,
    DISK_QUERY,
    GRAPH_URL,
    MANAGED_GROUPS_QUERY,
    RUN_COMMAND_TIMEOUT,
    SKUS_TIMEOUT,
    SUBSCRIPTIONS_QUERY,
    VM_QUERY,
    AccessProfile,
    AgentPool,
    AksCluster,
    AzureCli,
    DataDiskRef,
    DiskRecord,
    ManagedGroup,
    MetricPoint,
    OtherDirectory,
    Subscription,
    VmRecord,
)
from zoorik.cloud.cli import Window, kubeconfig_file
from zoorik.cloud.started import StartedCommands
from zoorik.errors import CommandFailed, NotSignedIn, UnexpectedResponse

SIGNED_OUT = "ERROR: Please run 'az login' to setup account."
VM_ID = "/subscriptions/sub-one/resourceGroups/group-one/providers/Microsoft.Compute/virtualMachines/vm-one"
DISK_ID = "/subscriptions/sub-one/resourceGroups/group-one/providers/Microsoft.Compute/disks/disk-one"


def account(sub: str, name: str, tenant: str, state: str = "Enabled") -> Dict[str, object]:
    """Make one subscription of az account list."""
    return {"id": sub, "name": name, "tenantId": tenant, "state": state, "isDefault": False}


def pool(**changes: object) -> Dict[str, object]:
    """Make one agent pool of a cluster, with changes."""
    record: Dict[str, object] = {
        "name": "system",
        "mode": "System",
        "vmSize": "Standard_D4s_v5",
        "count": 3,
        "osType": "Linux",
        "availabilityZones": ["1", "2", "3"],
        "maxPods": 110,
        "nodeLabels": {"team": "one"},
        "nodeTaints": ["CriticalAddonsOnly=true:NoSchedule"],
        "currentOrchestratorVersion": "1.30.4",
    }
    record.update(changes)
    return record


def cluster(**changes: object) -> Dict[str, object]:
    """Make one record of az aks list, with changes."""
    record: Dict[str, object] = {
        "name": "cluster-one",
        "resourceGroup": "group-one",
        "location": "westeurope",
        "powerState": {"code": "Running"},
        "kubernetesVersion": "1.30.4",
        "apiServerAccessProfile": {"enablePrivateCluster": True, "disableRunCommand": False},
        "aadProfile": {"managed": True},
        "agentPoolProfiles": [pool()],
    }
    record.update(changes)
    return record


def without(record: Dict[str, object], key: str) -> Dict[str, object]:
    """Give record with key left out."""
    return {name: value for name, value in record.items() if name != key}


def graph_call(query: str, subscriptions: List[str], page: object, token: str = "") -> Call:
    """Make one Resource Graph call over subscriptions, for the page after token, and its answer."""
    options: Dict[str, object] = {"$top": 1000, "resultFormat": "objectArray"}
    if token:
        options = {**options, "$skipToken": token}
    body = json.dumps({"subscriptions": subscriptions, "query": query, "options": options})
    return answer(az("rest", "--method", "post", "--url", GRAPH_URL, "--body", body), page)


def rest_get(url: str, page: object) -> Call:
    """Make one az rest GET of url, and its answer."""
    return answer(az("rest", "--method", "get", "--url", url), page)


class ScopeTest(CloudTestCase):
    """The account scope."""

    def scope_calls(self, accounts: List[Dict[str, object]]) -> List[Call]:
        """Make the two account calls, signed in to the directory tenant-home."""
        return [
            answer(az("account", "show"), {"tenantId": "tenant-home", "id": "sub-one"}),
            answer(az("account", "list"), accounts),
        ]

    def test_signed_out_is_named_as_the_run_prints_it(self) -> None:
        """A signed-out az fails the scope as NotSignedIn, which the run prints as its Azure line."""
        host, _ = self.host([failing(az("account", "show"), SIGNED_OUT)])
        with self.assertRaises(NotSignedIn) as raised:
            AzureCli(host, StartedCommands()).scope()
        self.assertEqual(f"Azure: {raised.exception}", "Azure: not signed in to Azure; run az login")

    def test_only_the_enabled_subscriptions_of_the_signed_in_directory_are_read(self) -> None:
        """Disabled subscriptions are left out, and the rest are sorted by name."""
        accounts = [
            account("sub-two", "Beta", "tenant-home"),
            account("sub-one", "Alpha", "tenant-home"),
            account("sub-off", "Gamma", "tenant-home", state="Disabled"),
            account("sub-warned", "Delta", "tenant-home", state="Warned"),
        ]
        host, runner = self.host(self.scope_calls(accounts))
        scope = AzureCli(host, StartedCommands()).scope()
        self.assertEqual(
            scope.subscriptions,
            (Subscription("sub-one", "Alpha", "tenant-home"), Subscription("sub-two", "Beta", "tenant-home")),
        )
        self.assertEqual(scope.elsewhere, ())
        self.assertEqual(runner.argvs, [az("account", "show"), az("account", "list")])

    def test_other_directories_are_named_once_each(self) -> None:
        """Enabled subscriptions of other directories are not read; each directory is named once, with how to add it."""
        accounts = [
            account("sub-one", "Alpha", "tenant-home"),
            account("sub-away", "Echo", "tenant-other"),
            account("sub-away-two", "Foxtrot", "tenant-other"),
            account("sub-far", "Golf", "tenant-third"),
            account("sub-far-off", "Hotel", "tenant-third", state="Disabled"),
        ]
        host, _ = self.host(self.scope_calls(accounts))
        scope = AzureCli(host, StartedCommands()).scope()
        self.assertEqual(scope.subscriptions, (Subscription("sub-one", "Alpha", "tenant-home"),))
        self.assertEqual([other.tenant for other in scope.elsewhere], ["tenant-other", "tenant-third"])
        self.assertEqual(
            [other.note for other in scope.elsewhere],
            [
                "2 subscriptions in another Azure directory; to include them, run az login --tenant tenant-other "
                "and run this again",
                "1 subscription in another Azure directory; to include it, run az login --tenant tenant-third "
                "and run this again",
            ],
        )
        self.assertEqual(
            scope.elsewhere[1],
            OtherDirectory("tenant-third", (Subscription("sub-far", "Golf", "tenant-third"),)),
        )

    def test_the_directory_is_named_by_its_name_and_domain_else_its_id(self) -> None:
        """The directory is labelled by its name and domain from az account list, else by its id."""
        named = {**account("sub-one", "Alpha", "tenant-home"), "tenantDisplayName": "Example"}
        with_domain = {**named, "tenantDefaultDomain": "example.test"}
        for accounts, label in (
            ([with_domain], "Example (example.test)"),
            ([named], "Example"),
            ([account("sub-one", "Alpha", "tenant-home")], "tenant-home"),
        ):
            host, _ = self.host(self.scope_calls(accounts))
            self.assertEqual(AzureCli(host, StartedCommands()).scope().label, label)


class ClusterTest(CloudTestCase):
    """AKS records, credentials and command invoke."""

    def read(self, *records: Dict[str, object]) -> List[AksCluster]:
        """Read the clusters of sub-one from records."""
        host, runner = self.host([answer(az("aks", "list", "--subscription", "sub-one"), list(records))])
        clusters = AzureCli(host, StartedCommands()).clusters("sub-one")
        self.assertEqual(runner.runs[0][1], CLUSTERS_TIMEOUT)
        return clusters

    def test_a_cluster_record_is_read_in_full(self) -> None:
        """Every field the run uses comes from the record."""
        self.assertEqual(
            self.read(cluster()),
            [
                AksCluster(
                    subscription="sub-one",
                    name="cluster-one",
                    resource_group="group-one",
                    location="westeurope",
                    power="Running",
                    access=AccessProfile(private_cluster=True, authorized_ip_ranges=None, run_command_disabled=False),
                    entra_id=True,
                    pools=(AgentPool(name="system", mode="System", priority=None, max_pods=110),),
                )
            ],
        )

    def test_a_public_cluster_has_no_access_profile(self) -> None:
        """apiServerAccessProfile, null or left out on a public cluster with no access settings, reads as None."""
        for record in (cluster(apiServerAccessProfile=None), without(cluster(), "apiServerAccessProfile")):
            self.assertIsNone(self.read(record)[0].access)

    def test_authorized_ranges_are_read(self) -> None:
        """A cluster limited to authorized IP ranges keeps them."""
        access = {"authorizedIpRanges": ["192.0.2.0/24"], "enablePrivateCluster": False}
        self.assertEqual(
            self.read(cluster(apiServerAccessProfile=access))[0].access,
            AccessProfile(private_cluster=False, authorized_ip_ranges=("192.0.2.0/24",), run_command_disabled=None),
        )

    def test_a_cluster_without_entra_id_has_no_aad_profile(self) -> None:
        """aadProfile, null without Entra ID integration, reads as no Entra ID."""
        self.assertFalse(self.read(cluster(aadProfile=None))[0].entra_id)
        self.assertFalse(self.read(without(cluster(), "aadProfile"))[0].entra_id)

    def test_a_regular_pool_has_no_priority(self) -> None:
        """scaleSetPriority, which Azure leaves out for a regular pool, reads as None."""
        first, second = self.read(cluster(agentPoolProfiles=[pool(scaleSetPriority="Spot"), pool()]))[0].pools
        self.assertEqual((first.priority, second.priority), ("Spot", None))

    def test_a_malformed_record_is_unexpected(self) -> None:
        """A required field left out, or of another type, raises UnexpectedResponse naming it."""
        with self.assertRaisesRegex(UnexpectedResponse, r"az aks list\[0\]\.powerState is missing"):
            self.read(without(cluster(), "powerState"))
        with self.assertRaisesRegex(UnexpectedResponse, r"agentPoolProfiles\[0\]\.maxPods is not a whole number"):
            self.read(cluster(agentPoolProfiles=[pool(maxPods="110")]))

    def test_credentials_go_to_the_clusters_own_kubeconfig(self) -> None:
        """get-credentials writes the cluster's kubeconfig in the temporary folder and answers with no JSON."""
        path = kubeconfig_file(self.temp_root, ("aks", "sub-one", "group-one", "cluster-one"))
        argv = az(
            "aks",
            "get-credentials",
            "--subscription",
            "sub-one",
            "--resource-group",
            "group-one",
            "--name",
            "cluster-one",
            "--file",
            "<temp>/kube/" + os.path.basename(path),
            "--overwrite-existing",
        )
        host, _ = self.host([answer(argv, None)])
        record = self.read(cluster())[0]
        self.assertEqual(AzureCli(host, StartedCommands()).write_kubeconfig(record), path)

    def test_command_invoke_gives_the_logs_and_is_kept_as_started(self) -> None:
        """A command invoke that succeeds gives its logs, and the run keeps that it started in the cluster."""
        record = self.read(cluster())[0]
        argv = az(
            "aks",
            "command",
            "invoke",
            "--subscription",
            "sub-one",
            "--resource-group",
            "group-one",
            "--name",
            "cluster-one",
            "--command",
            "kubectl get nodes",
        )
        succeeded = {"provisioningState": "Succeeded", "exitCode": 0, "logs": "node-one Ready\n", "reason": None}
        host, runner = self.host([answer(argv, succeeded)])
        started = StartedCommands()
        self.assertEqual(AzureCli(host, started).invoke(record, "kubectl get nodes", 90.0), "node-one Ready\n")
        self.assertEqual(runner.runs[0][1], 90.0)
        self.assertEqual(
            started.note(),
            "az aks command invoke had already started in 1 cluster (see the activity log); "
            "nothing else in your cloud was changed.",
        )

    def test_a_command_invoke_that_did_not_succeed_is_named(self) -> None:
        """A failed command invoke is named by its reason, or by its state when it gives none."""
        record = self.read(cluster())[0]
        argv = az(
            "aks",
            "command",
            "invoke",
            "--subscription",
            "sub-one",
            "--resource-group",
            "group-one",
            "--name",
            "cluster-one",
            "--command",
            "true",
        )
        host, _ = self.host(
            [
                answer(argv, {"provisioningState": "Failed", "reason": "(ResourceNotFound) the pod is gone"}),
                answer(argv, {"provisioningState": "Canceled", "reason": None}),
            ]
        )
        cli = AzureCli(host, StartedCommands())
        with self.assertRaisesRegex(CommandFailed, r"^not found$"):
            cli.invoke(record, "true", 60.0)
        with self.assertRaisesRegex(CommandFailed, r"^command invoke ended as Canceled$"):
            cli.invoke(record, "true", 60.0)


class ResourceGraphTest(CloudTestCase):
    """Resource Graph reads."""

    def test_pages_are_followed(self) -> None:
        """Each page's skip token asks for the next page, until a page has none."""
        subscriptions = ["sub-one", "sub-two"]
        host, _ = self.host(
            [
                graph_call(
                    SUBSCRIPTIONS_QUERY, subscriptions, {"data": [{"subscriptionId": "sub-one"}], "$skipToken": "next"}
                ),
                graph_call(SUBSCRIPTIONS_QUERY, subscriptions, {"data": [{"subscriptionId": "sub-two"}]}, token="next"),
            ]
        )
        self.assertEqual(
            AzureCli(host, StartedCommands()).readable_subscriptions(subscriptions), frozenset(subscriptions)
        )

    def test_subscriptions_go_1000_to_a_query(self) -> None:
        """A query over 1,001 subscriptions is made twice: for the first 1,000, then for the last one."""
        subscriptions = [f"sub-{number}" for number in range(1001)]
        host, runner = self.host(
            [
                graph_call(SUBSCRIPTIONS_QUERY, subscriptions[:1000], {"data": [{"subscriptionId": "sub-0"}]}),
                graph_call(SUBSCRIPTIONS_QUERY, subscriptions[1000:], {"data": []}),
            ]
        )
        self.assertEqual(AzureCli(host, StartedCommands()).readable_subscriptions(subscriptions), frozenset(["sub-0"]))
        self.assertEqual(len(runner.runs), 2)

    def test_vms_are_read_with_their_data_disks(self) -> None:
        """A VM in no zone has zones None; an unmanaged data disk has no managed disk."""
        rows = [
            {
                "id": VM_ID,
                "name": "vm-one",
                "resourceGroup": "group-one",
                "subscriptionId": "sub-one",
                "location": "westeurope",
                "zones": ["2"],
                "size": "Standard_D4s_v5",
                "osType": "Linux",
                "power": "PowerState/running",
                "dataDisks": [
                    {"lun": 0, "managedDisk": {"id": DISK_ID}, "diskSizeGB": 128},
                    {"lun": 1, "vhd": {"uri": "https://example.test/disk.vhd"}, "diskSizeGB": 64},
                ],
            },
            {
                "id": VM_ID + "-two",
                "name": "vm-two",
                "resourceGroup": "group-one",
                "subscriptionId": "sub-one",
                "location": "westeurope",
                "zones": None,
                "size": "Standard_B2s",
                "osType": "Windows",
                "power": "",
                "dataDisks": [],
            },
        ]
        host, _ = self.host([graph_call(VM_QUERY, ["sub-one"], {"data": rows})])
        first, second = AzureCli(host, StartedCommands()).virtual_machines(["sub-one"])
        self.assertEqual(first.zones, ("2",))
        self.assertEqual(first.data_disks, (DataDiskRef(0, DISK_ID, 128), DataDiskRef(1, None, 64)))
        self.assertEqual(
            second,
            VmRecord(
                id=VM_ID + "-two",
                name="vm-two",
                resource_group="group-one",
                subscription="sub-one",
                location="westeurope",
                zones=None,
                size="Standard_B2s",
                os_type="Windows",
                power="",
                data_disks=(),
            ),
        )

    def test_disks_and_managed_groups_are_read(self) -> None:
        """A disk's performance figures are None where it has none; managed groups name their manager."""
        disk = {
            "id": DISK_ID,
            "subscriptionId": "sub-one",
            "location": "westeurope",
            "sku": "Premium_LRS",
            "tier": "P10",
            "sizeGb": 128,
            "iops": None,
            "mbps": None,
            "ade": None,
            "maxShares": None,
        }
        group = {"subscriptionId": "sub-one", "name": "group-nodes", "managedBy": "/subscriptions/sub-one/clusters/one"}
        host, _ = self.host(
            [
                graph_call(DISK_QUERY, ["sub-one"], {"data": [disk]}),
                graph_call(MANAGED_GROUPS_QUERY, ["sub-one"], {"data": [group]}),
            ]
        )
        cli = AzureCli(host, StartedCommands())
        self.assertEqual(
            cli.managed_disks(["sub-one"]),
            [DiskRecord(DISK_ID, "sub-one", "westeurope", "Premium_LRS", "P10", 128, None, None, None, None)],
        )
        self.assertEqual(
            cli.managed_groups(["sub-one"]),
            [ManagedGroup("sub-one", "group-nodes", "/subscriptions/sub-one/clusters/one")],
        )


class RunCommandTest(CloudTestCase):
    """Run Command in a VM."""

    def argv(self, script: str) -> List[str]:
        """Give the Run Command call for script in the made-up VM."""
        return az("vm", "run-command", "invoke", "--ids", VM_ID, "--command-id", "RunShellScript", "--scripts", script)

    def test_what_the_script_printed_is_given_and_the_start_is_kept(self) -> None:
        """The text between the stdout and stderr marks is the answer; the run keeps that it started."""
        message = "Enable succeeded: \n[stdout]\nS\nK 6.8.0\n\n[stderr]\nwarning\n"
        host, runner = self.host(
            [answer(self.argv("echo S"), {"value": [{"code": "ProvisioningState/succeeded", "message": message}]})]
        )
        started = StartedCommands()
        self.assertEqual(AzureCli(host, started).run_shell_script(VM_ID, "echo S"), "\nS\nK 6.8.0\n\n")
        self.assertEqual(runner.runs[0][1], RUN_COMMAND_TIMEOUT)
        self.assertIn("Azure Run Command had already started in 1 VM", started.note())

    def test_an_answer_without_the_stdout_mark_is_unexpected(self) -> None:
        """Run Command's answer always marks the script's output; one without the mark is unexpected."""
        host, _ = self.host([answer(self.argv("echo S"), {"value": [{"message": "Enable succeeded"}]})])
        with self.assertRaisesRegex(UnexpectedResponse, r"gave no \[stdout\] part"):
            AzureCli(host, StartedCommands()).run_shell_script(VM_ID, "echo S")


class MonitorTest(CloudTestCase):
    """Azure Monitor metrics."""

    def test_the_busiest_minute_of_each_hour_is_read(self) -> None:
        """Each hour gives its Maximum; an hour with no data gives None."""
        url = (
            DISK_ID + "/providers/Microsoft.Insights/metrics?api-version=2023-10-01"
            "&metricnames=Composite%20Disk%20Read%20Operations%2Fsec%2CComposite%20Disk%20Write%20Operations%2Fsec"
            "&timespan=2026-01-04T09%3A00%3A00Z%2F2026-01-05T09%3A00%3A00Z&interval=PT1H&aggregation=Maximum"
        )
        series = {
            "data": [{"timeStamp": "2026-01-04T09:00:00Z", "maximum": 120}, {"timeStamp": "2026-01-04T10:00:00Z"}]
        }
        metric = {"name": {"value": "Composite Disk Read Operations/sec"}, "timeseries": [series]}
        host, _ = self.host([rest_get(url, {"value": [metric]})])
        window = Window(datetime(2026, 1, 4, 9, tzinfo=timezone.utc), datetime(2026, 1, 5, 9, tzinfo=timezone.utc))
        metrics = ["Composite Disk Read Operations/sec", "Composite Disk Write Operations/sec"]
        self.assertEqual(
            AzureCli(host, StartedCommands()).monitor_maxima(DISK_ID, metrics, window),
            [
                MetricPoint("Composite Disk Read Operations/sec", "2026-01-04T09:00:00Z", 120.0),
                MetricPoint("Composite Disk Read Operations/sec", "2026-01-04T10:00:00Z", None),
            ],
        )


class ComputeSkusTest(CloudTestCase):
    """The Compute SKUs of a region."""

    URL = (
        "/subscriptions/sub-one/providers/Microsoft.Compute/skus?api-version=2021-07-01"
        "&%24filter=location%20eq%20%27westeurope%27"
    )

    def sku(self, resource_type: str, name: str, **changes: object) -> Dict[str, object]:
        """Make one Compute SKU, offered in zones 1 to 3 with no restrictions, with changes."""
        record: Dict[str, object] = {
            "resourceType": resource_type,
            "name": name,
            "locations": ["westeurope"],
            "locationInfo": [{"location": "westeurope", "zones": ["1", "2", "3"]}],
            "capabilities": [],
            "restrictions": [],
        }
        record.update(changes)
        return record

    def test_sizes_and_disk_types_are_read_once_per_region(self) -> None:
        """Both pages are read, other kinds of SKU are left out, and a second ask reads nothing."""
        capabilities = [
            {"name": "vCPUs", "value": "4"},
            {"name": "MemoryGB", "value": "16"},
            {"name": "CpuArchitectureType", "value": "x64"},
            {"name": "PremiumIO", "value": "True"},
            {"name": "MaxDataDiskCount", "value": "8"},
            {"name": "UncachedDiskIOPS", "value": "6400"},
            {"name": "UncachedDiskBytesPerSecond", "value": "145000000"},
        ]
        zonal = self.sku(
            "virtualMachines",
            "Standard_D4s_v5",
            family="standardDSv5Family",
            capabilities=capabilities,
            locationInfo=[
                {
                    "location": "westeurope",
                    "zones": ["1", "2", "3"],
                    "zoneDetails": [{"name": ["2"], "capabilities": [{"name": "UltraSSDAvailable", "value": "True"}]}],
                }
            ],
            restrictions=[{"type": "Zone", "restrictionInfo": {"zones": ["3"], "locations": ["westeurope"]}}],
        )
        burstable = self.sku(
            "virtualMachines",
            "Standard_B2s",
            family="standardBSFamily",
            restrictions=[{"type": "Location", "restrictionInfo": {"locations": ["westeurope"]}}],
        )
        gpu = self.sku("virtualMachines", "Standard_NC6s_v3", capabilities=[{"name": "GPUs", "value": "1"}])
        page_two = self.URL + "&%24skiptoken=two"
        host, runner = self.host(
            [
                rest_get(
                    self.URL,
                    {"value": [zonal, burstable, self.sku("availabilitySets", "Aligned")], "nextLink": page_two},
                ),
                rest_get(page_two, {"value": [gpu, self.sku("disks", "PremiumV2_LRS")]}),
            ]
        )
        cli = AzureCli(host, StartedCommands())
        skus = cli.compute_skus("sub-one", "westeurope")
        self.assertIs(cli.compute_skus("sub-one", "westeurope"), skus)
        self.assertEqual([run[1] for run in runner.runs], [SKUS_TIMEOUT, SKUS_TIMEOUT])
        self.assertEqual(sorted(skus.vm_sizes), ["standard_b2s", "standard_d4s_v5", "standard_nc6s_v3"])
        size = skus.vm_sizes["standard_d4s_v5"]
        self.assertEqual(
            (size.vcpus, size.memory_gb, size.architecture, size.premium_io, size.max_data_disks),
            (4, 16.0, "x64", True, 8),
        )
        self.assertEqual((size.uncached_iops, size.uncached_mbps, size.gpus, size.burstable), (6400, 145, 0, False))
        self.assertEqual((size.zones, size.offered, size.ultra_ssd), (frozenset({"1", "2"}), True, True))
        small = skus.vm_sizes["standard_b2s"]
        self.assertEqual((small.zones, small.offered, small.burstable, small.vcpus), (frozenset(), False, True, None))
        self.assertEqual(skus.vm_sizes["standard_nc6s_v3"].gpus, 1)
        self.assertEqual(skus.disk_types["PremiumV2_LRS"].zones, frozenset({"1", "2", "3"}))

    def test_a_capability_of_another_form_is_unexpected(self) -> None:
        """A capability the run reads as a number or a flag, written otherwise, raises UnexpectedResponse."""
        odd = self.sku("virtualMachines", "Standard_X", capabilities=[{"name": "vCPUs", "value": "four"}])
        host, _ = self.host([rest_get(self.URL, {"value": [odd]})])
        size = AzureCli(host, StartedCommands()).compute_skus("sub-one", "westeurope").vm_sizes["standard_x"]
        with self.assertRaisesRegex(UnexpectedResponse, "the vCPUs capability of Standard_X"):
            _ = size.vcpus
