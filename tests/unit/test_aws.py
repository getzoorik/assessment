"""AWS through aws."""

import json
import os
from datetime import datetime, timezone
from typing import Dict, List

from tests.unit.clis import CloudTestCase, answer, aws, failing
from zoorik.cloud.aws import (
    INSTANCE_TYPES_TIMEOUT,
    PRICE_LIST_TIMEOUT,
    AttachedVolume,
    AwsCli,
    AwsScope,
    Datapoint,
    EbsVolume,
    Ec2Instance,
    EksCluster,
    Invocation,
    NodeGroup,
    PriceDimension,
    SpotPrice,
)
from zoorik.cloud.cli import Window, kubeconfig_file
from zoorik.cloud.started import StartedCommands
from zoorik.errors import AccessDenied, NotSignedIn, UnexpectedResponse

SIGNED_OUT = 'Unable to locate credentials. You can configure credentials by running "aws configure".'
IDENTITY = aws("sts", "get-caller-identity", "--region", "us-east-1")
REGIONS = aws("ec2", "describe-regions", "--query", "Regions[].RegionName", "--region", "us-east-1")


def node_group(name: str, **changes: object) -> Dict[str, object]:
    """Make one answer of aws eks describe-nodegroup, with changes."""
    record: Dict[str, object] = {
        "nodegroupName": name,
        "capacityType": "ON_DEMAND",
        "instanceTypes": ["m5.large"],
        "amiType": "AL2023_x86_64_STANDARD",
        "scalingConfig": {"minSize": 1, "maxSize": 3, "desiredSize": 2},
    }
    record.update(changes)
    return {"nodegroup": record}


def instance(instance_id: str, **changes: object) -> Dict[str, object]:
    """Make one instance of aws ec2 describe-instances, with changes."""
    record: Dict[str, object] = {
        "InstanceId": instance_id,
        "InstanceType": "m5.large",
        "State": {"Code": 16, "Name": "running"},
        "RootDeviceName": "/dev/xvda",
        "BlockDeviceMappings": [
            {"DeviceName": "/dev/xvda", "Ebs": {"VolumeId": "vol-root"}},
            {"DeviceName": "/dev/sdf", "Ebs": {"VolumeId": "vol-data"}},
        ],
        "NetworkInterfaces": [{"NetworkInterfaceId": "eni-one"}],
    }
    record.update(changes)
    return record


def price_item(attributes: Dict[str, str], dimensions: List[Dict[str, object]]) -> str:
    """Make one PriceList entry, a product with on-demand prices, as the text the Price List gives."""
    term = {
        "priceDimensions": {f"dimension-{index}": dimension for index, dimension in enumerate(dimensions)},
        "sku": "product-one",
    }
    return json.dumps({"product": {"attributes": attributes}, "terms": {"OnDemand": {"term-one": term}}})


class ScopeTest(CloudTestCase):
    """The account scope."""

    def test_signed_out_is_named_as_the_run_prints_it(self) -> None:
        """An aws with no credentials fails the scope as NotSignedIn, which the run prints as its AWS line."""
        host, _ = self.host([failing(IDENTITY, SIGNED_OUT)])
        with self.assertRaises(NotSignedIn) as raised:
            AwsCli(host, StartedCommands()).scope()
        self.assertEqual(f"AWS: {raised.exception}", "AWS: no AWS credentials; run aws configure or aws sso login")

    def test_every_enabled_region_is_read_and_account_calls_go_to_us_east_1(self) -> None:
        """The account and its regions come from us-east-1, whatever region the environment names."""
        host, runner = self.host(
            [answer(IDENTITY, {"Account": "account-one"}), answer(REGIONS, ["eu-west-1", "ap-south-1", "us-east-1"])]
        )
        scope = AwsCli(host, StartedCommands()).scope()
        self.assertEqual(scope, AwsScope("account-one", ("ap-south-1", "eu-west-1", "us-east-1")))
        self.assertEqual(runner.argvs, [IDENTITY, REGIONS])

    def test_a_region_list_failure_is_named_and_not_narrowed(self) -> None:
        """A region list that can't be read fails the scope, named; no region is read in its place."""
        denied = "An error occurred (UnauthorizedOperation) when calling the DescribeRegions operation"
        host, runner = self.host([answer(IDENTITY, {"Account": "account-one"}), failing(REGIONS, denied)])
        with self.assertRaisesRegex(AccessDenied, r"^could not list the account's regions: access denied$"):
            AwsCli(host, StartedCommands()).scope()
        self.assertEqual(len(runner.runs), 2)


class EksTest(CloudTestCase):
    """EKS clusters and node groups."""

    def test_cluster_names_are_read_once_per_region(self) -> None:
        """A region's clusters are listed once, sorted, however many times they are asked for."""
        host, runner = self.host(
            [answer(aws("eks", "list-clusters", "--region", "eu-west-1"), {"clusters": ["two", "one"]})]
        )
        cli = AwsCli(host, StartedCommands())
        self.assertEqual(cli.cluster_names("eu-west-1"), ("one", "two"))
        self.assertEqual(cli.cluster_names("eu-west-1"), ("one", "two"))
        self.assertEqual(len(runner.runs), 1)

    def test_a_cluster_record_is_read(self) -> None:
        """The cluster's status and whether its API server answers from the internet come from its record."""
        record = {
            "cluster": {
                "name": "cluster-one",
                "status": "ACTIVE",
                "version": "1.31",
                "resourcesVpcConfig": {"endpointPublicAccess": False, "endpointPrivateAccess": True},
            }
        }
        host, _ = self.host(
            [answer(aws("eks", "describe-cluster", "--name", "cluster-one", "--region", "eu-west-1"), record)]
        )
        self.assertEqual(
            AwsCli(host, StartedCommands()).cluster("eu-west-1", "cluster-one"),
            EksCluster("eu-west-1", "cluster-one", "ACTIVE", public_endpoint=False),
        )

    def test_a_malformed_cluster_record_is_unexpected(self) -> None:
        """A record without its network settings raises UnexpectedResponse."""
        record = {"cluster": {"name": "cluster-one", "status": "ACTIVE", "version": "1.31"}}
        host, _ = self.host(
            [answer(aws("eks", "describe-cluster", "--name", "cluster-one", "--region", "eu-west-1"), record)]
        )
        with self.assertRaisesRegex(UnexpectedResponse, r"cluster\.resourcesVpcConfig is missing"):
            AwsCli(host, StartedCommands()).cluster("eu-west-1", "cluster-one")

    def test_node_groups_are_read_in_name_order(self) -> None:
        """Each node group is described, in name order, with its capacity type."""

        def describe(name: str) -> List[str]:
            return aws(
                "eks",
                "describe-nodegroup",
                "--cluster-name",
                "cluster-one",
                "--nodegroup-name",
                name,
                "--region",
                "eu-west-1",
            )

        host, _ = self.host(
            [
                answer(
                    aws("eks", "list-nodegroups", "--cluster-name", "cluster-one", "--region", "eu-west-1"),
                    {"nodegroups": ["spot", "general"]},
                ),
                answer(describe("general"), node_group("general")),
                answer(describe("spot"), node_group("spot", capacityType="SPOT")),
            ]
        )
        self.assertEqual(
            AwsCli(host, StartedCommands()).node_groups("eu-west-1", "cluster-one"),
            [NodeGroup("general", "ON_DEMAND"), NodeGroup("spot", "SPOT")],
        )

    def test_the_kubeconfig_goes_to_the_clusters_own_file(self) -> None:
        """update-kubeconfig writes the cluster's kubeconfig in the temporary folder."""
        path = kubeconfig_file(self.temp_root, ("eks", "eu-west-1", "cluster-one"))
        argv = aws(
            "eks",
            "update-kubeconfig",
            "--name",
            "cluster-one",
            "--kubeconfig",
            "<temp>/kube/" + os.path.basename(path),
            "--region",
            "eu-west-1",
        )
        host, _ = self.host([answer(argv, None)])
        self.assertEqual(AwsCli(host, StartedCommands()).write_kubeconfig("eu-west-1", "cluster-one"), path)


class Ec2Test(CloudTestCase):
    """EC2 instances, volumes and instance types."""

    def test_instances_are_read_and_an_untagged_instance_has_no_tags(self) -> None:
        """Tags, Platform and InstanceLifecycle, which AWS leaves out where they don't apply, read as None."""
        tagged = instance("i-one", Tags=[{"Key": "Name", "Value": "web"}], Platform="windows", InstanceLifecycle="spot")
        reservations = {"Reservations": [{"Instances": [tagged]}, {"Instances": [instance("i-two")]}]}
        argv = aws(
            "ec2",
            "describe-instances",
            "--filters",
            "Name=instance-state-name,Values=pending,running,stopping,stopped",
            "--region",
            "eu-west-1",
        )
        host, _ = self.host([answer(argv, reservations)])
        first, second = AwsCli(host, StartedCommands()).instances("eu-west-1")
        self.assertEqual((first.tags, first.platform, first.lifecycle), ({"Name": "web"}, "windows", "spot"))
        self.assertEqual(
            second,
            Ec2Instance(
                id="i-two",
                instance_type="m5.large",
                state="running",
                platform=None,
                tags=None,
                lifecycle=None,
                root_device="/dev/xvda",
                volumes=(AttachedVolume("/dev/xvda", "vol-root"), AttachedVolume("/dev/sdf", "vol-data")),
                interfaces=1,
            ),
        )

    def test_instances_are_read_once_per_region(self) -> None:
        """Both parts ask for a region's instances, and the region is read once."""
        argv = aws(
            "ec2",
            "describe-instances",
            "--filters",
            "Name=instance-state-name,Values=pending,running,stopping,stopped",
            "--region",
            "eu-west-1",
        )
        host, runner = self.host([answer(argv, {"Reservations": [{"Instances": [instance("i-one")]}]})])
        cli = AwsCli(host, StartedCommands())
        self.assertEqual(cli.instances("eu-west-1"), cli.instances("eu-west-1"))
        self.assertEqual(len(runner.runs), 1)

    def test_volumes_are_read(self) -> None:
        """IOPS and throughput are None where the volume type has none."""
        volumes = {
            "Volumes": [
                {
                    "VolumeId": "vol-one",
                    "VolumeType": "gp3",
                    "Size": 100,
                    "Iops": 3000,
                    "Throughput": 125,
                    "MultiAttachEnabled": False,
                },
                {"VolumeId": "vol-two", "VolumeType": "st1", "Size": 500, "MultiAttachEnabled": False},
            ]
        }
        host, _ = self.host([answer(aws("ec2", "describe-volumes", "--region", "eu-west-1"), volumes)])
        self.assertEqual(
            AwsCli(host, StartedCommands()).volumes("eu-west-1"),
            [EbsVolume("vol-one", "gp3", 100, 3000, 125, False), EbsVolume("vol-two", "st1", 500, None, None, False)],
        )

    def test_instance_types_and_their_zones_are_read_once_per_region(self) -> None:
        """Each region's types are read once with their limits; the parts AWS gives for some types only may be None.

        A type's network interfaces are those of its default network card.
        """
        full = {
            "InstanceType": "m5d.large",
            "VCpuInfo": {"DefaultVCpus": 2},
            "MemoryInfo": {"SizeInMiB": 8192},
            "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
            "BurstablePerformanceSupported": False,
            "GpuInfo": {"Gpus": [{"Count": 1}, {"Count": 2}]},
            "EbsInfo": {
                "MaximumEbsAttachments": 27,
                "AttachmentLimitType": "shared",
                "NvmeSupport": "required",
                "EbsOptimizedInfo": {"BaselineIops": 3600, "BaselineThroughputInMBps": 81.25},
            },
            "InstanceStorageInfo": {"Disks": [{"Count": 1, "SizeInGB": 75}], "NvmeSupport": "required"},
            "NetworkInfo": {
                "MaximumNetworkInterfaces": 6,
                "Ipv4AddressesPerInterface": 10,
                "DefaultNetworkCardIndex": 1,
                "NetworkCards": [
                    {"NetworkCardIndex": 0, "MaximumNetworkInterfaces": 4},
                    {"NetworkCardIndex": 1, "MaximumNetworkInterfaces": 3},
                ],
            },
        }
        small = {
            "InstanceType": "t3.micro",
            "VCpuInfo": {"DefaultVCpus": 2},
            "MemoryInfo": {"SizeInMiB": 1024},
            "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
            "BurstablePerformanceSupported": True,
            "EbsInfo": {"NvmeSupport": "required"},
            "NetworkInfo": {
                "MaximumNetworkInterfaces": 2,
                "Ipv4AddressesPerInterface": 2,
                "DefaultNetworkCardIndex": 0,
                "NetworkCards": [{"NetworkCardIndex": 0, "MaximumNetworkInterfaces": 2}],
            },
        }
        offerings = {
            "InstanceTypeOfferings": [
                {"InstanceType": "m5d.large", "Location": "eu-west-1b"},
                {"InstanceType": "m5d.large", "Location": "eu-west-1a"},
                {"InstanceType": "t3.micro", "Location": "eu-west-1c"},
            ]
        }
        host, runner = self.host(
            [
                answer(
                    aws("ec2", "describe-instance-types", "--region", "eu-west-1"), {"InstanceTypes": [full, small]}
                ),
                answer(
                    aws(
                        "ec2",
                        "describe-instance-type-offerings",
                        "--location-type",
                        "availability-zone",
                        "--region",
                        "eu-west-1",
                    ),
                    offerings,
                ),
            ]
        )
        cli = AwsCli(host, StartedCommands())
        types = cli.instance_types("eu-west-1")
        self.assertIs(cli.instance_types("eu-west-1"), types)
        large, micro = types["m5d.large"], types["t3.micro"]
        self.assertEqual((large.vcpus, large.memory_mib, large.architectures, large.gpus), (2, 8192, ("x86_64",), 3))
        self.assertEqual((large.max_attachments, large.attachment_limit, large.ebs_nvme), (27, "shared", "required"))
        self.assertEqual((large.baseline_iops, large.baseline_mbps, large.store_disks), (3600, 81.25, 1))
        self.assertEqual((large.store_nvme, large.max_interfaces, large.ipv4_per_interface), ("required", 3, 10))
        self.assertEqual(
            (micro.burstable, micro.gpus, micro.max_attachments, micro.baseline_iops), (True, 0, None, None)
        )
        self.assertEqual((micro.store_disks, micro.store_nvme), (0, None))
        zones = cli.type_zones("eu-west-1")
        self.assertEqual(dict(zones), {"m5d.large": ("eu-west-1a", "eu-west-1b"), "t3.micro": ("eu-west-1c",)})
        self.assertIs(cli.type_zones("eu-west-1"), zones)
        self.assertEqual([run[1] for run in runner.runs], [INSTANCE_TYPES_TIMEOUT, INSTANCE_TYPES_TIMEOUT])


class SystemsManagerTest(CloudTestCase):
    """Systems Manager Run Command."""

    def test_online_instances_are_named(self) -> None:
        """Only instances whose agent is online are listed."""
        argv = aws(
            "ssm",
            "describe-instance-information",
            "--filters",
            "Key=PingStatus,Values=Online",
            "Key=ResourceType,Values=EC2Instance",
            "--region",
            "eu-west-1",
        )
        host, _ = self.host([answer(argv, {"InstanceInformationList": [{"InstanceId": "i-one"}]})])
        self.assertEqual(AwsCli(host, StartedCommands()).ssm_online("eu-west-1"), frozenset({"i-one"}))

    def test_a_sent_command_is_kept_as_started_and_read_until_it_arrives(self) -> None:
        """The command's id comes back; its invocation is None until it reaches the instance, then it is read."""
        parameters = json.dumps({"commands": ["echo S", "echo E"], "executionTimeout": ["120"]})
        send = aws(
            "ssm",
            "send-command",
            "--instance-ids",
            "i-one",
            "--document-name",
            "AWS-RunShellScript",
            "--comment",
            "Zoorik disk assessment: reads disk usage",
            "--timeout-seconds",
            "300",
            "--parameters",
            parameters,
            "--region",
            "eu-west-1",
        )
        invocation = aws(
            "ssm",
            "get-command-invocation",
            "--command-id",
            "command-one",
            "--instance-id",
            "i-one",
            "--region",
            "eu-west-1",
        )
        not_yet = "An error occurred (InvocationDoesNotExist) when calling the GetCommandInvocation operation"
        done = {"Status": "Success", "StatusDetails": "Success", "StandardOutputContent": "S\nE\n"}
        host, _ = self.host(
            [
                answer(send, {"Command": {"CommandId": "command-one"}}),
                failing(invocation, not_yet),
                answer(invocation, done),
            ]
        )
        started = StartedCommands()
        cli = AwsCli(host, started)
        self.assertEqual(cli.send_command("eu-west-1", "i-one", "echo S\necho E"), "command-one")
        self.assertIn("Systems Manager Run Command had already started in 1 instance", started.note())
        self.assertIsNone(cli.command_invocation("eu-west-1", "command-one", "i-one"))
        self.assertEqual(
            cli.command_invocation("eu-west-1", "command-one", "i-one"), Invocation("Success", "Success", "S\nE\n")
        )


class MetricsAndPricesTest(CloudTestCase):
    """CloudWatch, the Price List and spot price history."""

    def test_the_maximum_of_each_hour_is_read(self) -> None:
        """Each datapoint gives its hour and its Maximum."""
        argv = aws(
            "cloudwatch",
            "get-metric-statistics",
            "--namespace",
            "AWS/EBS",
            "--metric-name",
            "VolumeReadOps",
            "--dimensions",
            "Name=VolumeId,Value=vol-one",
            "--start-time",
            "2026-01-04T09:00:00Z",
            "--end-time",
            "2026-01-05T09:00:00Z",
            "--period",
            "3600",
            "--statistics",
            "Maximum",
            "--region",
            "eu-west-1",
        )
        points = {"Datapoints": [{"Timestamp": "2026-01-04T09:00:00Z", "Maximum": 600.0, "Unit": "Count"}]}
        host, _ = self.host([answer(argv, points)])
        window = Window(datetime(2026, 1, 4, 9, tzinfo=timezone.utc), datetime(2026, 1, 5, 9, tzinfo=timezone.utc))
        self.assertEqual(
            AwsCli(host, StartedCommands()).volume_maxima("eu-west-1", "vol-one", "VolumeReadOps", window),
            [Datapoint("2026-01-04T09:00:00Z", 600.0)],
        )

    def test_price_products_are_parsed_with_their_on_demand_prices(self) -> None:
        """Each PriceList entry is parsed; a product with no on-demand terms has no on-demand prices."""
        filters = json.dumps(
            [
                {"Type": "TERM_MATCH", "Field": "regionCode", "Value": "eu-west-1"},
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
            ]
        )
        argv = aws(
            "pricing",
            "get-products",
            "--service-code",
            "AmazonEC2",
            "--format-version",
            "aws_v1",
            "--filters",
            filters,
            "--region",
            "us-east-1",
        )
        priced = price_item(
            {"volumeApiName": "gp3"}, [{"unit": "GB-Mo", "pricePerUnit": {"USD": "0.0880000000"}, "beginRange": "0"}]
        )
        reserved_only = json.dumps({"product": {"attributes": {"volumeApiName": "io2"}}, "terms": {"Reserved": {}}})
        host, runner = self.host([answer(argv, {"PriceList": [priced, reserved_only], "FormatVersion": "aws_v1"})])
        first, second = AwsCli(host, StartedCommands()).price_products(
            (("regionCode", "eu-west-1"), ("productFamily", "Storage"))
        )
        self.assertEqual(first.attributes, {"volumeApiName": "gp3"})
        self.assertEqual(first.on_demand, (PriceDimension(0.088, "GB-Mo"),))
        self.assertEqual(second.on_demand, ())
        self.assertEqual(runner.runs[0][1], PRICE_LIST_TIMEOUT)

    def test_a_price_list_entry_that_is_not_json_is_unexpected(self) -> None:
        """Each PriceList entry is JSON text; one that is not raises UnexpectedResponse."""
        filters = json.dumps([{"Type": "TERM_MATCH", "Field": "regionCode", "Value": "eu-west-1"}])
        argv = aws(
            "pricing",
            "get-products",
            "--service-code",
            "AmazonEC2",
            "--format-version",
            "aws_v1",
            "--filters",
            filters,
            "--region",
            "us-east-1",
        )
        host, _ = self.host([answer(argv, {"PriceList": ["{not json"]})])
        with self.assertRaisesRegex(UnexpectedResponse, "aws pricing get-products gave no JSON"):
            AwsCli(host, StartedCommands()).price_products((("regionCode", "eu-west-1"),))

    def test_spot_prices_are_read_for_each_zone(self) -> None:
        """One read gives the region's history: each entry's machine type, zone, price and when it took effect."""
        argv = aws(
            "ec2",
            "describe-spot-price-history",
            "--product-descriptions",
            "Linux/UNIX",
            "--start-time",
            "2026-01-05T09:30:00Z",
            "--region",
            "eu-west-1",
        )
        entry = {"InstanceType": "m5.large", "AvailabilityZone": "eu-west-1a", "SpotPrice": "0.035", "Timestamp": "t1"}
        host, _ = self.host([answer(argv, {"SpotPriceHistory": [entry]})])
        at = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(
            AwsCli(host, StartedCommands()).spot_prices("eu-west-1", at),
            [SpotPrice("m5.large", "eu-west-1a", 0.035, "t1")],
        )

    def test_every_call_names_its_region(self) -> None:
        """No aws call takes its region from the environment or a profile."""
        host, runner = self.host(
            [
                answer(IDENTITY, {"Account": "account-one"}),
                answer(REGIONS, ["eu-west-1"]),
                answer(aws("eks", "list-clusters", "--region", "eu-west-1"), {"clusters": []}),
                answer(aws("ec2", "describe-volumes", "--region", "eu-west-1"), {"Volumes": []}),
            ]
        )
        cli = AwsCli(host, StartedCommands())
        cli.scope()
        cli.cluster_names("eu-west-1")
        cli.volumes("eu-west-1")
        for argv in runner.argvs:
            self.assertIn("--region", argv)
