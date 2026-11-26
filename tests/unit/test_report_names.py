"""The names the report shows: each as it is, or under --anonymize as a hash made fresh for each run."""

import unittest

from zoorik.cloud.keys import AWS, AZURE
from zoorik.report.names import Kind, Names

SUBSCRIPTIONS = {"sub-id": "subscription-one"}


class NamesTest(unittest.TestCase):
    """Names."""

    def test_without_anonymize_every_name_is_as_it_is(self) -> None:
        """Clusters, subscriptions by name, accounts by id and mount points all show as they are."""
        names = Names("0123456789abcdef", anonymize=False)
        self.assertEqual(names.name(Kind.CLUSTER, "cluster-one"), "cluster-one")
        self.assertEqual(names.account(AZURE, "sub-id", SUBSCRIPTIONS), "subscription-one")
        self.assertEqual(names.account(AWS, "123456789012", SUBSCRIPTIONS), "123456789012")
        self.assertEqual(names.mounts("/data is 90% full"), "/data is 90% full")

    def test_a_hash_is_the_kind_and_eight_hex_digits_the_same_all_through_a_run(self) -> None:
        """A name hashes the same way every time in one run, and another way with another run's salt."""
        names = Names("0123456789abcdef", anonymize=True)
        hashed = names.name(Kind.CLUSTER, "cluster-one")
        self.assertRegex(hashed, r"^cluster-[0-9a-f]{8}$")
        self.assertEqual(names.name(Kind.CLUSTER, "cluster-one"), hashed)
        self.assertNotEqual(Names("fedcba9876543210", anonymize=True).name(Kind.CLUSTER, "cluster-one"), hashed)

    def test_a_name_is_hashed_within_what_it_is_unique_in(self) -> None:
        """Two workloads called web in two namespaces get two hashes."""
        names = Names("0123456789abcdef", anonymize=True)
        self.assertNotEqual(names.name(Kind.WORKLOAD, "web", "shop"), names.name(Kind.WORKLOAD, "web", "tools"))

    def test_accounts_are_hashed_as_subscriptions_or_accounts(self) -> None:
        """An Azure subscription hashes its name, and an AWS account its id."""
        names = Names("0123456789abcdef", anonymize=True)
        self.assertEqual(
            names.account(AZURE, "sub-id", SUBSCRIPTIONS), names.name(Kind.SUBSCRIPTION, "subscription-one")
        )
        self.assertRegex(names.account(AWS, "123456789012", SUBSCRIPTIONS), r"^acct-[0-9a-f]{8}$")

    def test_only_the_mount_points_of_a_reason_are_hashed(self) -> None:
        """Mount points become hashes; a machine type and a fraction stay as they are."""
        names = Names("0123456789abcdef", anonymize=True)
        hidden = names.mounts("/data and /var/lib/app are 90% full; m4.xlarge has 1/2 of its slots free")
        mount = r"mount-[0-9a-f]{8}"
        self.assertRegex(hidden, rf"^{mount} and {mount} are 90% full; m4\.xlarge has 1/2 of its slots free$")
        self.assertEqual(names.mounts("/data is full"), f"{names.name(Kind.MOUNT, '/data')} is full")
