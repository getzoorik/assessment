"""Pools: their members, their size and performance, and their price, as the disk method lays them out."""

import unittest
from typing import List, Tuple

from zoorik.cloud.keys import AWS, AZURE
from zoorik.disks.model import AzureLimits, EbsLimits, Load
from zoorik.disks.pools import (
    MEGABYTE,
    TOO_BUSY,
    TOO_MUCH_DATA,
    AzureRoom,
    Demand,
    Gp3Layout,
    NotEstimated,
    Overloaded,
    Pool,
    aws_pool,
    azure_pool,
    evacuation_cap,
    full_geometry,
    gp3_layout,
    gp3_members,
    gp3_need,
    hold_gib,
    member_iops,
    member_mbps,
    peak_of,
    pool_members,
    pool_size_gib,
    tier_down,
    tier_up,
)
from zoorik.format import GIB
from zoorik.pricing.disks import DiskSpec

D8S = AzureLimits(slots=16, uncached_iops=12800, uncached_mbps=290, v2=True)
NITRO = EbsLimits(slots=21, free_slots=20, uncached_iops=12000, uncached_mbps=312, nitro=True)


class PricePerGib:
    """A price list that charges a dollar a month for each GiB, and keeps each disk it prices."""

    def __init__(self) -> None:
        """Keep no disk yet."""
        self.disks: List[Tuple[str, DiskSpec]] = []

    def monthly(self, region: str, disk: DiskSpec) -> float:
        """Keep the disk, and give its size in dollars."""
        self.disks.append((region, disk))
        return float(disk.size_gb)


class SizingTest(unittest.TestCase):
    """The pool's size, and the sizes its members may take."""

    def test_the_pool_holds_the_data_and_20_percent_more_at_least_50_gib(self) -> None:
        """250 GiB of data take 300; 100 take 150 (20 is less than 50); 1,000 take 1,200."""
        self.assertEqual([pool_size_gib(used) for used in (250, 100, 1000)], [300, 150, 1200])

    def test_premium_ssd_members_take_its_sizes_and_premium_ssd_v2_any(self) -> None:
        """Premium SSD rounds up or down to its sizes, within the smallest and largest; Premium SSD v2 keeps any."""
        self.assertEqual((tier_up(100, False), tier_down(100, False)), (128, 64))
        self.assertEqual((tier_up(40000, False), tier_down(2, False)), (32767, 4))
        self.assertEqual((tier_up(100, True), tier_down(100, True)), (100, 100))

    def test_a_member_is_no_larger_than_the_vm_empties_in_the_evacuation_window(self) -> None:
        """30% of 290 MB/s for 120 minutes, members at most 80% full, two at once: 382 GiB; unknown without a speed."""
        self.assertEqual((evacuation_cap(290), evacuation_cap(None)), (382, None))


class AzureLayoutTest(unittest.TestCase):
    """The members of an Azure pool."""

    def test_a_small_pool_is_three_to_six_equal_elastics(self) -> None:
        """300 GiB on Premium SSD v2: five of 64 GiB; 150 GiB on Premium SSD: three of 128 (its smallest member)."""
        self.assertEqual(full_geometry(300, 250, 290, True), [64] * 5)
        self.assertEqual(full_geometry(150, 100, 290, False), [128] * 3)

    def test_a_pool_of_512_gib_or_more_has_two_anchors(self) -> None:
        """1,200 GiB: anchors of half the smaller of 75% of the pool and its data, then elastics to cover the rest."""
        self.assertEqual(full_geometry(1200, 1000, 290, True), [450, 450, 75, 75, 75, 75])
        self.assertEqual(full_geometry(1200, 1000, 290, False), [256, 256] + [128] * 6)

    def test_premium_ssd_v2_adds_the_small_member_when_the_slots_allow(self) -> None:
        """Six members fit the 13 slots a 16-slot VM with two disks leaves free, one kept spare."""
        self.assertEqual(pool_members(AzureRoom(300, 250, 16, 2, 290, True)), ([64] * 5, 1))

    def test_fewer_free_slots_give_one_anchor_and_fewer_elastics(self) -> None:
        """With 3 free slots: an anchor of 225 GiB, one elastic of 75 and the small member."""
        self.assertEqual(pool_members(AzureRoom(300, 250, 8, 4, 290, True)), ([225, 75], 1))

    def test_a_vm_whose_slots_cannot_hold_a_pool_gets_none(self) -> None:
        """Fewer than 8 slots, or fewer than 2 free for members, hold no pool."""
        self.assertIsNone(pool_members(AzureRoom(300, 250, 6, 2, 290, True)))
        self.assertIsNone(pool_members(AzureRoom(300, 250, 16, 14, 290, True)))

    def test_a_premium_ssd_v2_member_takes_its_share_of_the_peak_within_its_limits(self) -> None:
        """10,000 IOPS x 1.3 shared by two is 6,500 each, held to 6,400 by the VM; 400 MB/s gives 260, held to 145."""
        self.assertEqual((member_iops(10000, 2, 64, None), member_iops(10000, 2, 64, 12800)), (6500, 6400))
        self.assertEqual((member_mbps(400, 2, 6400, None), member_mbps(400, 2, 6400, 290)), (260, 145))
        self.assertEqual((member_iops(100, 6, 64, 12800), member_mbps(10, 6, 3000, 290)), (3000, 125))


class AzurePoolTest(unittest.TestCase):
    """azure_pool."""

    def test_a_premium_ssd_v2_pool_is_priced_member_by_member(self) -> None:
        """250 GiB of data: five 64 GiB members and the 8 GiB one, each at its free 3,000 IOPS and 125 MB/s."""
        prices = PricePerGib()
        demand = Demand((200 * GIB, 50 * GIB), Load(600, 34, 250, 12))
        self.assertEqual(azure_pool(prices, "westeurope", 2, D8S, demand), Pool(328, 328.0))
        member = DiskSpec("PremiumV2_LRS", 64, "", 3000, 125)
        expected = [member] * 5 + [DiskSpec("PremiumV2_LRS", 8, "", 3000, 125)]
        self.assertEqual(prices.disks, [("westeurope", disk) for disk in expected])

    def test_a_premium_ssd_pool_is_priced_at_its_members_sizes(self) -> None:
        """100 GiB of data on Premium SSD: three 128 GiB members."""
        prices = PricePerGib()
        limits = AzureLimits(16, 12800, 290, False)
        pool = azure_pool(prices, "westeurope", 1, limits, Demand((100 * GIB,), Load(500, 30, 200, 10)))
        self.assertEqual(pool, Pool(384, 384.0))
        self.assertEqual([disk for _, disk in prices.disks], [DiskSpec("Premium_LRS", 128, "", None, None)] * 3)

    def test_a_premium_ssd_pool_that_cannot_carry_the_peak_is_overloaded(self) -> None:
        """Three 128 GiB members give 1,500 IOPS; 5,000 x 1.3 is more."""
        limits = AzureLimits(16, 12800, 290, False)
        with self.assertRaisesRegex(Overloaded, f"^{TOO_BUSY}$"):
            azure_pool(PricePerGib(), "westeurope", 1, limits, Demand((100 * GIB,), Load(5000, 30, 200, 10)))

    def test_data_beyond_what_the_members_hold_is_not_estimated(self) -> None:
        """40,000 GiB need a 48,000 GiB pool, more than two anchors and six elastics hold."""
        with self.assertRaisesRegex(NotEstimated, f"^{TOO_MUCH_DATA}$"):
            azure_pool(PricePerGib(), "westeurope", 1, D8S, Demand((40000 * GIB,), Load(600, 34, 250, 12)))

    def test_a_vm_whose_free_slots_cannot_hold_the_pool_gets_none(self) -> None:
        """Fourteen disks on a 16-slot VM leave one slot for members."""
        self.assertIsNone(azure_pool(PricePerGib(), "westeurope", 14, D8S, Demand((100 * GIB,), Load(1, 1, 1, 1))))


class AwsPoolTest(unittest.TestCase):
    """The gp3 pool of an instance."""

    def test_a_new_pool_is_held_at_most_90_percent_full_with_10_gib_free(self) -> None:
        """100 GiB of data: 103 GiB with btrfs's 3%, held at 103 / 0.9, 115 GiB."""
        self.assertEqual(hold_gib(100 * GIB), 115)

    def test_the_pool_takes_at_least_two_equal_volumes_within_the_free_slots(self) -> None:
        """115 GiB on two volumes of 58; no pool with one free slot, or with only one slot beside the spare one."""
        self.assertEqual(gp3_members(NITRO, 100 * GIB), (2, 58))
        no_slot = EbsLimits(slots=2, free_slots=1, uncached_iops=None, uncached_mbps=None, nitro=True)
        one_slot = EbsLimits(slots=3, free_slots=2, uncached_iops=None, uncached_mbps=None, nitro=True)
        self.assertEqual((gp3_members(no_slot, 100 * GIB), gp3_members(one_slot, 100 * GIB)), (None, None))

    def test_each_volume_carries_its_share_of_the_peak_and_the_write_peak(self) -> None:
        """243 MiB/s x 1.3 shared by two is 159 MiB/s each, above the write peak's 106; 3,000 IOPS carry it."""
        self.assertEqual(gp3_need(Load(4300, 243.19, 1600, 81.06), 2), (3000, 159))
        self.assertEqual(gp3_need(Load(200, 14.3, 100, 4.8), 2), (3000, 125))

    def test_the_layout_is_held_to_what_the_instance_drives(self) -> None:
        """159 MiB/s each is held to 156, half the type's 312 MiB/s."""
        layout = gp3_layout(NITRO, 440 * GIB, Load(4300, 243.19, 1600, 81.06))
        self.assertEqual(layout, Gp3Layout(count=2, size=252, iops=3000, mbps=156))

    def test_a_pool_too_busy_or_too_large_is_not_estimated(self) -> None:
        """Two 58 GiB volumes top out at 29,000 IOPS; a volume above 16,384 GiB is not made."""
        with self.assertRaisesRegex(Overloaded, f"^{TOO_BUSY}$"):
            gp3_layout(NITRO, 100 * GIB, Load(200000, 10, 0, 0))
        small = EbsLimits(slots=4, free_slots=3, uncached_iops=None, uncached_mbps=None, nitro=True)
        with self.assertRaisesRegex(NotEstimated, f"^{TOO_MUCH_DATA}$"):
            gp3_layout(small, 40000 * GIB, Load(1, 1, 1, 1))

    def test_the_pool_is_priced_volume_by_volume(self) -> None:
        """Two 58 GiB gp3 volumes at 3,000 IOPS and 125 MiB/s."""
        prices = PricePerGib()
        pool = aws_pool(prices, "eu-west-1", NITRO, Demand((100 * GIB,), Load(200, 14.3, 100, 4.8)))
        self.assertEqual(pool, Pool(116, 116.0))
        self.assertEqual(prices.disks, [("eu-west-1", DiskSpec("gp3", 58, "", 3000, 125))])


class PeakTest(unittest.TestCase):
    """peak_of."""

    def test_disks_are_added_hour_by_hour_then_each_figure_takes_its_busiest_hour(self) -> None:
        """Two disks' hours add up; the busiest IOPS and the busiest throughput may come from different hours."""
        one = {"10:00": Load(300, 20e6, 200, 10e6), "11:00": Load(100, 40e6, 50, 30e6)}
        two = {"10:00": Load(300, 2e6, 50, 2e6)}
        self.assertEqual(peak_of([one, two], MEGABYTE[AZURE]), Load(600, 40, 250, 30))
        self.assertEqual(peak_of([{"10:00": Load(1, 2**20, 1, 2**20)}], MEGABYTE[AWS]), Load(1, 1, 1, 1))
