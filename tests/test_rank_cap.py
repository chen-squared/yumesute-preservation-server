"""Player rank cap release and the actor level cap it gates.

Master-data evidence: AuditionRewardPackageMaster grants item 610001 once per audition
phase 2 (250 auditions), the official initial player rank limit is 50, and PlayerRankMaster
releases ranks up to 300. Nothing in the pinned upstream raises User.playerRankLimit, so
these checks pin the release arithmetic and the banked-EXP behaviour it unlocks.
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
    RANK_CAP_ITEM,
    actor_experience,
    actor_level_cap,
    bulk_step,
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


class TestRelease(unittest.TestCase):
    def test_release_plan_spends_one_item_per_level(self):
        self.assertEqual(release_plan(50, 1, 300), (51, 1))
        self.assertEqual(release_plan(269, 3, 300), (272, 3))
        self.assertEqual(release_plan(299, 5, 300), (300, 1))
        self.assertEqual(release_plan(300, 5, 300), (300, 0))
        self.assertEqual(release_plan(269, 0, 300), (269, 0))

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
