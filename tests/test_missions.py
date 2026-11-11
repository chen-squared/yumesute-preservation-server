"""The daily and weekly missions: the master's own conditions, cycles and chains.

MissionMaster states each condition in its ``description``, so the ids the code counts are
pinned against that text instead of remembered: 100000 スタミナを300消費, 100200 稽古を1回,
100300 全てのデイリーミッションをクリア, 100400 ジュエルショップの毎日無料パック, 200000
スタミナを3000消費, 200100 演劇リーグを1回, 200200 スターアクトを5回, 200300 フォトを3枚現像,
200400 フォトのレベルを合計3, 200500 マーケット（レギュラー）で10回交換, 200600
「全てのデイリーミッションをクリアしよう」を5回. A counter wired to the wrong id would
otherwise sit quietly on a mission the player never completes.

The cycles are read from the archive too. The daily rows open 2023-08-26T05:00 and the
weekly rows 2023-10-16T05:00 - a Monday - both at the hour ``helpers/daily.py`` resets the
daily allowances, and ``cycle_start`` keeps that phase: daily at 05:00 JST, weekly on
Monday's 05:00 JST boundary. Nothing in the archive gives a different weekly instant.

100300 is the reason the daily set matters: its goal is 3, and exactly three daily missions
besides it are still on sale - 100100 フォトを1枚現像 closed on 2023-10-11 and stays closed,
so counting it would make the mission impossible. 200400 (photo levels) stays unprogressed
because photo enhancement is still a stub upstream; no counter pretends otherwise.
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

import missions

JST = dt.timezone(dt.timedelta(hours=9))


def jst(year, month, day, hour=12, minute=0):
    """Epoch micros of a JST wall-clock instant."""
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=JST).timestamp() * 1_000_000)


def mission(ident):
    return next(m for m in cache.mission_master if m.id_ == ident)


def stage(ident):
    m = mission(ident)
    return sorted(m.stages or [], key=lambda x: x.mission_stage_order)[0]


class Conditions(unittest.TestCase):
    """Every counted id has to be the mission whose text says what is being counted."""

    COUNTED = {
        100000: "スタミナ",
        100200: "稽古",
        100300: "全てのデイリー",
        100400: "毎日無料パック",
        200000: "スタミナ",
        200100: "演劇リーグ",
        200200: "スターアクト",
        200300: "現像",
        200400: "フォトのレベル",
        200500: "マーケット",
        200600: "デイリー",
    }

    def test_each_counted_id_matches_its_own_description(self):
        for ident, needle in self.COUNTED.items():
            self.assertIn(needle, mission(ident).description or "",
                          f"mission {ident} does not say {needle!r}")

    def test_the_counted_ids_are_exactly_the_daily_and_weekly_sets(self):
        self.assertEqual({100000, 100100, 100200, 100300, 100400},
                         {m.id_ for m in cache.mission_master if int(m.mission_category) == missions.DAILY})
        self.assertEqual({200000, 200100, 200200, 200300, 200400, 200500, 200600},
                         {m.id_ for m in cache.mission_master if int(m.mission_category) == missions.WEEKLY})

    def test_the_goals_are_the_numbers_the_code_has_to_reach(self):
        goals = {ident: stage(ident).stage_goal_value for ident in self.COUNTED}
        self.assertEqual(
            {100000: 300, 100200: 1, 100300: 3, 100400: 1, 200000: 3000, 200100: 1,
             200200: 5, 200300: 3, 200400: 3, 200500: 10, 200600: 5}, goals)

    def test_both_stamina_missions_track_the_same_number(self):
        self.assertEqual((missions.STAMINA[missions.DAILY], missions.STAMINA[missions.WEEKLY]),
                         (100000, 200000))
        self.assertEqual(stage(100000).stage_goal_value * 10, stage(200000).stage_goal_value)


class Cycles(unittest.TestCase):
    """Where a cycle begins, taken from the archive's own anchors."""

    def test_a_day_starts_at_five_in_the_morning_jst(self):
        for hour in (5, 12, 23):
            start = missions.cycle_start(missions.DAILY, jst(2026, 11, 9, hour))
            self.assertEqual(dt.datetime.fromtimestamp(start / 1e6, JST),
                             dt.datetime(2026, 11, 9, 5, tzinfo=JST))
        # Before five o'clock the account is still in yesterday's cycle.
        for hour in (0, 4):
            self.assertEqual(dt.datetime.fromtimestamp(
                missions.cycle_start(missions.DAILY, jst(2026, 11, 9, hour)) / 1e6, JST),
                dt.datetime(2026, 11, 8, 5, tzinfo=JST))

    def test_a_week_starts_on_monday_at_the_daily_boundary(self):
        # 2026-11-09 is a Monday; 2026-11-08 a Sunday.
        for hour in (5, 12, 23):
            self.assertEqual(dt.datetime.fromtimestamp(
                missions.cycle_start(missions.WEEKLY, jst(2026, 11, 9, hour)) / 1e6, JST),
                dt.datetime(2026, 11, 9, 5, tzinfo=JST))
        for hour in (0, 4, 23):
            self.assertEqual(dt.datetime.fromtimestamp(
                missions.cycle_start(missions.WEEKLY, jst(2026, 11, 8, hour)) / 1e6, JST),
                dt.datetime(2026, 11, 2, 5, tzinfo=JST))

    def test_the_weekly_phase_is_the_archives_not_an_invention(self):
        """The weekly rows opened on a Monday, at the daily boundary's own hour."""
        weekly = dt.datetime.fromisoformat(mission(200000).start_date)
        daily = dt.datetime.fromisoformat(mission(100000).start_date)
        self.assertEqual((weekly.weekday(), weekly.hour, weekly.minute), (0, 5, 0),
                         f"the weekly set opened {mission(200000).start_date}, "
                         "which is not a Monday at the reset hour")
        self.assertEqual((daily.hour, daily.minute), (weekly.hour, weekly.minute),
                         "the daily and weekly rows do not share one clock")
        boundary = jst(2023, 10, 16, 5)
        week = 7 * 86400 * 1_000_000
        self.assertEqual(missions.cycle_start(missions.WEEKLY, boundary), boundary)
        self.assertEqual(missions.cycle_start(missions.WEEKLY, boundary + week - 1), boundary)
        self.assertEqual(missions.cycle_start(missions.WEEKLY, boundary + week), boundary + week)

    def test_the_day_starts_where_the_daily_rows_say_they_do(self):
        """``helpers/daily.py`` resets at 05:00 JST; the master's daily rows carry hour 05."""
        daily = dt.datetime.fromisoformat(mission(100000).start_date)
        boundary = dt.datetime.fromtimestamp(
            missions.cycle_start(missions.DAILY, jst(2026, 11, 9)) / 1e6, JST)
        self.assertEqual((boundary.hour, boundary.minute), (daily.hour, daily.minute))

    def test_the_closed_daily_mission_is_never_issued(self):
        now = jst(2026, 11, 9)
        self.assertEqual([m.id_ for m in missions.issued(missions.DAILY, now)],
                         [100000, 100200, 100300, 100400])
        self.assertEqual([m.id_ for m in missions.issued(missions.WEEKLY, now)],
                         [200000, 200100, 200200, 200300, 200400, 200500, 200600])
        # 100300's goal is the number of other daily missions on sale, so it can be cleared.
        others = [m.id_ for m in missions.issued(missions.DAILY, now) if m.id_ != missions.ALL_DAILIES]
        self.assertEqual(stage(missions.ALL_DAILIES).stage_goal_value, len(others))

    def test_a_fresh_issue_starts_on_the_latest_started_stage(self):
        for ident in (100000, 200500):
            self.assertEqual(missions.current_stage(mission(ident), jst(2026, 11, 9)).id_,
                             stage(ident).id_)


class Chains(unittest.TestCase):
    """The two missions that count other missions."""

    def test_a_cleared_daily_feeds_the_all_daily_counter(self):
        for ident in (100000, 100200, 100400):
            self.assertEqual(missions.chain_target(missions.DAILY, ident), missions.ALL_DAILIES)

    def test_the_all_daily_counter_feeds_the_weekly_one(self):
        self.assertEqual(missions.chain_target(missions.DAILY, missions.ALL_DAILIES),
                         missions.ALL_DAILIES_WEEK)

    def test_weekly_missions_feed_nothing(self):
        for m in missions.issued(missions.WEEKLY, jst(2026, 11, 9)):
            self.assertIsNone(missions.chain_target(int(m.mission_category), m.id_))

    def test_the_weekly_counter_wants_five_days_not_five_lives(self):
        self.assertIn("全てのデイリーミッションをクリアしよう", mission(missions.ALL_DAILIES_WEEK).description)
        self.assertEqual(stage(missions.ALL_DAILIES_WEEK).stage_goal_value, 5)


class DailyFreePack(unittest.TestCase):
    """The jewel-shop row mission 100400 is about."""

    def setUp(self):
        self.item = next(i for i in cache.jewel_shop_item_master if i.id_ == missions.FREE_PACK_ITEM)

    def test_the_row_is_the_daily_free_pack_and_the_only_one(self):
        self.assertEqual(self.item.name, "毎日無料パック")
        self.assertEqual([i.id_ for i in cache.jewel_shop_item_master if "毎日" in (i.name or "")],
                         [missions.FREE_PACK_ITEM])

    def test_it_is_free_and_limited_to_one_purchase(self):
        self.assertEqual(int(self.item.purchase_type), 3)
        self.assertEqual(int(self.item.purchase_value), 0)
        self.assertEqual(int(self.item.purchase_limit), 1)
        self.assertTrue(missions.on_sale(self.item, jst(2026, 11, 9)))

    def test_the_mission_jumps_to_that_shop_category(self):
        m = mission(missions.FREE_PACK)
        self.assertEqual(int(m.jump_type), 3)  # JumpTypes.JewelShop
        self.assertEqual(int(m.jump_target_id), int(self.item.m_jewel_shop_category_id))


if __name__ == "__main__":
    unittest.main()
