"""The jewel shop's own rules, read out of JewelShopItemMaster.

The daily free pack is the reason this exists: mission 100400 asks for it, the master says
it is free and one per day, and its lineup is スタミナ60 + コイン10000. Pinning those numbers
here is what keeps a grant that silently becomes "nothing" or a cap that quietly stops
biting visible.
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "vendor/server-of-dreams"))

from helpers.cache import cache, load_master_data

load_master_data()

import jewel_shop
import missions

JST = dt.timezone(dt.timedelta(hours=9))
MICRO = 1_000_000
DAY = 86_400 * MICRO


def jst(year, month, day, hour=12, minute=0):
    """Epoch micros of a JST wall-clock instant."""
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=JST).timestamp() * MICRO)


def item(ident):
    return next(i for i in cache.jewel_shop_item_master if i.id_ == ident)


def local(micros):
    return dt.datetime.fromtimestamp(micros / MICRO, JST)


class FreePacks(unittest.TestCase):
    """The 25 rows ``purchase_type`` Free: free, one each, and always with a lineup."""

    def setUp(self):
        self.free = [i for i in cache.jewel_shop_item_master
                     if int(i.purchase_type) == jewel_shop.FREE]

    def test_there_are_25_of_them_and_none_of_them_carries_a_jewel_ledger(self):
        self.assertEqual(len(self.free), 25)
        self.assertTrue(all(not i.give_paid_jewel for i in self.free),
                        "a free pack with give_paid_jewel would have been paid out by the "
                        "grant this module replaced")

    def test_every_one_of_them_is_limited_and_has_contents_to_hand_over(self):
        self.assertTrue(all(int(i.purchase_limit) == 1 for i in self.free))
        for i in self.free:
            self.assertTrue(jewel_shop.lineup(i), f"{i.id_} {i.name} has no lineup")

    def test_they_are_daily_or_lifetime_and_never_another_clock(self):
        self.assertEqual({int(i.replace_types or 0) for i in self.free},
                         {jewel_shop.LIFETIME, int(jewel_shop.ShopReplaceTypes.Daily)})
        self.assertEqual([i.id_ for i in self.free if int(i.replace_types or 0)
                          == int(jewel_shop.ShopReplaceTypes.Daily)], [4101])

    def test_the_daily_free_pack_is_4101_and_its_contents_are_stamina_and_coin(self):
        pack = item(4101)
        self.assertEqual(pack.name, "毎日無料パック")
        self.assertEqual(jewel_shop.granted(pack),
                         [(15, 0, 60), (12, 0, 10_000)])   # ThingTypes Stamina, Coin
        self.assertEqual(int(pack.purchase_limit), 1)
        self.assertEqual(int(pack.replace_types), int(jewel_shop.ShopReplaceTypes.Daily))

    def test_the_pack_mission_asks_for_is_that_row(self):
        m = next(x for x in cache.mission_master if x.id_ == missions.FREE_PACK)
        self.assertIn("毎日無料パック", m.description or "")
        self.assertEqual(missions.FREE_PACK_ITEM, 4101)


class PaidPacks(unittest.TestCase):
    """Left as upstream granted them, with the gap between ledger and lineup recorded."""

    def test_a_paid_pack_still_grants_its_paid_jewels(self):
        pack = item(11)                       # 【1回限定】劇ジュエルパックA
        self.assertEqual(jewel_shop.granted(pack), [(13, 0, 120)])

    def test_that_is_less_than_the_lineup_this_server_still_cannot_pay_out(self):
        """11 sends 130 jewels: 120 paid plus a bonus, and freeJewel is all there is."""
        pack = item(11)
        self.assertEqual(jewel_shop.lineup(pack), [(13, 0, 130)])
        self.assertGreater(sum(q for _, _, q in jewel_shop.lineup(pack)),
                           sum(q for _, _, q in jewel_shop.granted(pack)),
                           "if the paid/free jewel split ever lands, this test says to "
                           "switch the paid rows over to their lineup as well")

    def test_six_rows_have_no_lineup_at_all(self):
        without = [i for i in cache.jewel_shop_item_master if not i.lineup]
        self.assertEqual(len(without), 6, [i.id_ for i in without])
        self.assertTrue(all(int(i.purchase_type) != jewel_shop.FREE for i in without))


class Caps(unittest.TestCase):
    """When a pack's cap resets, and which caps this server refuses to read."""

    def test_a_daily_cap_resets_on_the_same_boundary_as_the_daily_missions(self):
        for when in (jst(2026, 11, 11), jst(2026, 11, 11, 4, 59), jst(2026, 11, 11, 23, 59)):
            end = jewel_shop.period_end(int(jewel_shop.ShopReplaceTypes.Daily), when)
            self.assertEqual(end - DAY, missions.cycle_start(missions.DAILY, when),
                             "the shop's day and the mission day have to be the same day")
            self.assertGreater(end, when, "a cap cannot reset before it was taken")
        # Midday today: the pack is open until tomorrow's boundary. At 04:59 the account is
        # still in yesterday's cycle, so its stamp is today's 05:00 - a second tap after
        # that is allowed.
        self.assertEqual(local(jewel_shop.period_end(
            int(jewel_shop.ShopReplaceTypes.Daily), jst(2026, 11, 11))),
            dt.datetime(2026, 11, 12, 5, tzinfo=JST))
        self.assertEqual(local(jewel_shop.period_end(
            int(jewel_shop.ShopReplaceTypes.Daily), jst(2026, 11, 11, 4, 59))),
            dt.datetime(2026, 11, 11, 5, tzinfo=JST))

    def test_a_weekly_cap_resets_on_the_next_monday_boundary(self):
        end = jewel_shop.period_end(int(jewel_shop.ShopReplaceTypes.Weekly), jst(2026, 11, 11))
        self.assertEqual(local(end), dt.datetime(2026, 11, 16, 5, tzinfo=JST))

    def test_a_monthly_cap_resets_on_the_first_at_the_same_hour(self):
        monthly = int(jewel_shop.ShopReplaceTypes.Monthly)
        self.assertEqual(local(jewel_shop.period_end(monthly, jst(2026, 11, 11))),
                         dt.datetime(2026, 12, 1, 5, tzinfo=JST))
        # Just before the boundary, the month that is starting is still the one to end on.
        self.assertEqual(local(jewel_shop.period_end(monthly, jst(2026, 12, 1, 4, 59))),
                         dt.datetime(2026, 12, 1, 5, tzinfo=JST))

    def test_lifetime_caps_never_roll_over(self):
        self.assertIsNone(jewel_shop.period_end(jewel_shop.LIFETIME, jst(2026, 11, 11)))
        self.assertIsNone(jewel_shop.period_end(None, jst(2026, 11, 11)))

    def test_a_cap_tied_to_a_pass_expiry_is_left_unread(self):
        """DailyPassExpired rolls when the pass runs out; passes are not modelled here."""
        self.assertIsNone(jewel_shop.period_end(10, jst(2026, 11, 11)))
        self.assertEqual(int(item(2001).replace_types), 10)     # ログインパス

    def test_a_lifetime_pack_with_no_stamp_never_reads_as_a_spent_period(self):
        """The claim's reset test must not treat a NULL stamp as expired for these."""
        sql, args = jewel_shop.claim_purchase(7, 4301, 1, None, jst(2026, 11, 11), 5150)
        self.assertIn(f'COALESCE("rePurchaseDate", {2 ** 62}) <= $5', sql)
        self.assertIsNone(args[3])
        # A period pack's NULL stamp was written before instants were stamped, so that
        # period counts as spent and the cap restarts at 1 instead of adding to it.
        stamp = jewel_shop.period_end(int(jewel_shop.ShopReplaceTypes.Daily), jst(2026, 11, 11))
        sql, args = jewel_shop.claim_purchase(7, 4101, 1, stamp, jst(2026, 11, 11), 5150)
        self.assertIn('COALESCE("rePurchaseDate", 0) <= $5', sql)
        self.assertEqual(args[3], stamp)
        self.assertIn('AND (CASE WHEN COALESCE("rePurchaseDate"', sql)   # cap is in the WHERE


if __name__ == "__main__":
    unittest.main()
