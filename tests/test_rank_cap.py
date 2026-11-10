"""Player rank cap release and the actor level cap it gates.

Two independent mechanisms, both master-driven:

* AuditionRewardPackageMaster grants item 610001 once per audition phase 2 (250 auditions)
  and the server applies it on arrival, +1 rank each. The client has no way to spend that
  item (ApiClient only offers the payload-less Player/ReleasePlayerRankCap), and the
  recovered official account ends with no 610001 row, 219 phase-2 clears, and a limit of
  exactly 50 + 219.
* PlayerRankMaster.player_rank_cap_master_id on ranks 160..290 (in tens) points at a
  PlayerRankCapMaster purchase - coin plus two shards - that lifts the limit by a whole
  10-rank block. That is the dialog the client shows (PlayerRankCapReleaseHepler ->
  PlayerRankCapReleaseDialog), and its numbers are pinned below against a live capture.

PlayerRankMaster releases ranks up to 300 and nothing in the pinned upstream maintains
User.playerRankLimit, so these checks pin the arithmetic for both paths.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "vendor/server-of-dreams"))

from helpers.cache import cache, load_master_data

load_master_data()

from gameplay import (
    CAP_RELEASE_STEP,
    RANK_CAP_ITEM,
    actor_experience,
    actor_level_cap,
    bulk_step,
    cap_release_cost,
    experience_to_reach_for,
    is_rank_capped,
    max_released_rank,
    release_plan,
)
from helpers.character_level import released_max_level


class TestMasterEvidence(unittest.TestCase):
    def test_cap_up_items_span_the_rank_range(self):
        packages = {p.id_: p for p in cache.audition_reward_package_master}
        grants = sum(
            sum(
                1
                for thing in packages[phase.audition_reward_package_master_id].rewards or []
                if thing.thing_id == RANK_CAP_ITEM
            )
            for phase in cache.audition_phase_master
            if phase.phase == 2
        )
        self.assertEqual(grants, 250)
        # The official initial limit plus every audition grant reaches the released maximum.
        self.assertEqual(50 + grants, max_released_rank())

    def test_cap_up_item_is_a_standing_item(self):
        item = next(x for x in cache.item_master if x.id_ == RANK_CAP_ITEM)
        self.assertFalse(item.consumable)
        self.assertEqual(item.category, 61)

    def test_purchasable_blocks_are_the_tens_from_160(self):
        rows = {
            r.rank: r.player_rank_cap_master_id
            for r in cache.player_rank_master
            if r.player_rank_cap_master_id
        }
        self.assertEqual(sorted(rows), list(range(160, 300, 10)))
        self.assertEqual([rows[r] for r in range(160, 300, 10)], list(range(1, 15)))

    def test_rank_270_purchase_matches_the_release_dialog(self):
        # Live capture: ランクキャップ 270 > 280, 130022 338559/72000, 130032 95142/16000,
        # coin 278317545 > 110317545.
        coin, items, raised = cap_release_cost(270)
        self.assertEqual((coin, items, raised), (168000000, {130022: 72000, 130032: 16000}, 280))
        self.assertEqual(raised, 270 + CAP_RELEASE_STEP)
        self.assertEqual(278317545 - coin, 110317545)


class TestRelease(unittest.TestCase):
    def test_release_plan_spends_one_item_per_level(self):
        self.assertEqual(release_plan(50, 1, 300), (51, 1))
        self.assertEqual(release_plan(269, 3, 300), (272, 3))
        self.assertEqual(release_plan(299, 5, 300), (300, 1))
        self.assertEqual(release_plan(300, 5, 300), (300, 0))
        self.assertEqual(release_plan(269, 0, 300), (269, 0))

    def test_purchase_only_exists_on_a_blocked_limit(self):
        # A limit that is not a block edge, or one already at the released maximum, has
        # nothing to buy.
        self.assertIsNone(cap_release_cost(1))
        self.assertIsNone(cap_release_cost(150))
        self.assertIsNone(cap_release_cost(269))
        self.assertIsNone(cap_release_cost(271))
        self.assertIsNone(cap_release_cost(max_released_rank()))

    def test_purchase_raises_a_whole_block_and_stops_at_the_maximum(self):
        self.assertEqual(cap_release_cost(160)[2], 170)
        self.assertEqual(cap_release_cost(160)[1], {130021: 2000, 130031: 1000})
        self.assertEqual(cap_release_cost(290)[2], 300)
        self.assertEqual(cap_release_cost(290)[0], 726000000)

    def test_capped_state(self):
        self.assertTrue(is_rank_capped({"playerRank": 269, "playerRankLimit": 269}))
        self.assertFalse(is_rank_capped({"playerRank": 269, "playerRankLimit": 270}))
        self.assertFalse(is_rank_capped({"playerRank": 1, "playerRankLimit": 50}))


class TestActorLevelCap(unittest.TestCase):
    def test_rank_limits_then_the_released_curve(self):
        self.assertEqual(actor_level_cap({"playerRank": 269, "playerRankLimit": 269}), 269)
        # The limit is the ceiling on rank, not a separate level ceiling.
        self.assertEqual(actor_level_cap({"playerRank": 269, "playerRankLimit": 300}), 269)
        self.assertEqual(actor_level_cap({"playerRank": 1, "playerRankLimit": 50}), 1)
        self.assertEqual(
            actor_level_cap({"playerRank": 300, "playerRankLimit": 300}),
            min(300, released_max_level()),
        )


class TestBankedExperience(unittest.TestCase):
    def test_exp_held_at_the_cap_becomes_levels(self):
        # Level 269 costs 329000 EXP on the rarity-4 curve.
        self.assertEqual(actor_experience(269, 330000, 0, 269, 4), (269, 330000))
        self.assertEqual(actor_experience(269, 330000, 0, 270, 4), (270, 1000))
        self.assertEqual(experience_to_reach_for(269, 330000, 270, 4), 0)
        # A partial bank, like the level 269 save data, stays put.
        self.assertEqual(actor_experience(269, 19918, 0, 270, 4), (269, 19918))
        # Rarity scales the same curve: floor(329000 * 0.8) = 263200 for rarity 3.
        self.assertEqual(actor_experience(269, 264000, 0, 270, 3), (270, 800))

    def test_bulk_step_levels_from_the_bank_without_items(self):
        row = {"level": 269, "currentExperience": 330000}
        spend, level, experience, need = bulk_step({}, row, 270, 0, 4)
        self.assertEqual(spend, {})
        self.assertEqual(need, 0)
        self.assertEqual((level, experience), (270, 1000))

    def test_bulk_step_still_buys_missing_exp(self):
        row = {"level": 269, "currentExperience": 19918}
        spend, level, experience, need = bulk_step({}, row, 270, 0, 4)
        self.assertEqual((level, experience), (269, 19918))
        self.assertEqual(need, 309082)
        # With an affordable pool the same step spends it and levels the actor.
        scripts = {x.item_master_id: x for x in cache.character_experience_item_master}
        richest = max(scripts, key=lambda i: scripts[i].acquirable_experience)
        value = scripts[richest].acquirable_experience
        pool = {richest: -(-need // value)}
        spend, level, experience, need = bulk_step(pool, row, 270, 0, 4)
        self.assertGreater(spend.get(richest, 0), 0)
        self.assertEqual(level, 270)
        self.assertLess(experience, 329000)


if __name__ == "__main__":
    unittest.main()
