"""Player rank cap: audition grants inside the block a gate release has opened.

Two mechanisms chained together, both master-driven:

* AuditionRewardPackageMaster grants item 610001 once per audition phase 2 - 250 auditions -
  and each grant raises User.playerRankLimit by one from the initial 50. The client cannot
  spend that item (ApiClient offers only the payload-less Player/ReleasePlayerRankCap), and
  the recovered official account holds no 610001 row at all with 219 phase-2 clears.
* PlayerRankMaster carries player_rank_cap_master_id on ranks 160..290 in tens; paying that
  PlayerRankCapMaster cost releases the next ten ranks. The release itself does not move the
  limit, it only lifts the ceiling the audition grants run into.

The two line up exactly: groups 1-22 hold the 110 auditions that carry the initial 50 to the
first gate at 160, the 14 gates cover 140 ranks for the 140 auditions in groups 23-50, and
50 + 250 = 300 = the highest released rank. PlayerRankMaster also jumps max stamina by
exactly 11 at 161, 171, ... 291 - the first rank-up inside a released block - which is the
rule the release dialog spells out ("ランクキャップ解放を行った上でランクアップすると、
スタミナ上限が11上昇します"). Nothing in the pinned upstream maintains any of this, so these
checks pin the arithmetic for both paths.
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
    INITIAL_RANK_LIMIT,
    RANK_CAP_ITEM,
    actor_experience,
    actor_level_cap,
    bulk_step,
    cap_release_cost,
    cap_target,
    experience_to_reach_for,
    free_rank_ceiling,
    gate_at_limit,
    is_rank_capped,
    max_released_rank,
    released_ceiling,
)
from helpers.character_level import released_max_level

GATE_RANKS = list(range(160, 300, CAP_RELEASE_STEP))


class TestMasterEvidence(unittest.TestCase):
    def test_gates_are_the_tens_from_160(self):
        rows = {
            r.rank: r.player_rank_cap_master_id
            for r in cache.player_rank_master
            if r.player_rank_cap_master_id
        }
        self.assertEqual(sorted(rows), GATE_RANKS)
        self.assertEqual([rows[r] for r in GATE_RANKS], list(range(1, 15)))
        self.assertEqual(free_rank_ceiling(), 160)

    def test_auditions_and_gates_cover_the_same_ground(self):
        groups = {}
        for row in cache.audition_master:
            groups[row.audition_group_number] = groups.get(row.audition_group_number, 0) + 1
        self.assertEqual(len(groups), 50)
        free = sum(n for group, n in groups.items() if group <= 22)
        gated = sum(n for group, n in groups.items() if group >= 23)
        # The free stretch runs out exactly on the first gate; the gates cover the rest.
        self.assertEqual(free, free_rank_ceiling() - INITIAL_RANK_LIMIT)
        self.assertEqual(gated, len(GATE_RANKS) * CAP_RELEASE_STEP)
        # And every audition plus the initial limit lands on the highest released rank.
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
        self.assertEqual(grants, free + gated)
        self.assertEqual(INITIAL_RANK_LIMIT + grants, max_released_rank())

    def test_stamina_jumps_eleven_on_the_first_rank_of_a_released_block(self):
        rows = {r.rank: r.max_stamina for r in cache.player_rank_master}
        jumped = [rank for rank in range(2, max_released_rank() + 1)
                  if rows[rank] - rows[rank - 1] == 11]
        self.assertEqual(jumped, [rank + 1 for rank in GATE_RANKS])
        self.assertEqual(rows[270], 700)
        self.assertEqual(rows[271], 711)

    def test_cap_up_item_is_a_standing_item(self):
        item = next(x for x in cache.item_master if x.id_ == RANK_CAP_ITEM)
        self.assertFalse(item.consumable)
        self.assertEqual(item.category, 61)

    def test_gate_cost_matches_the_release_dialog(self):
        # Live capture at rank 270: 130022 338559/72000, 130032 95142/16000, and the coin
        # line 278317545 > 110317545.
        coin, items = cap_release_cost(gate_at_limit(270))
        self.assertEqual((coin, items), (168000000, {130022: 72000, 130032: 16000}))
        self.assertEqual(278317545 - coin, 110317545)


class TestCapArithmetic(unittest.TestCase):
    def test_gates_only_exist_on_the_block_edges(self):
        self.assertEqual(gate_at_limit(160), 1)
        self.assertEqual(gate_at_limit(270), 12)
        self.assertEqual(gate_at_limit(290), 14)
        for limit in (1, 50, 159, 161, 269, 271, 299):
            self.assertIsNone(gate_at_limit(limit))
        self.assertIsNone(gate_at_limit(max_released_rank()))

    def test_ceiling_follows_the_released_blocks(self):
        self.assertEqual(released_ceiling([], 300), 160)
        self.assertEqual(released_ceiling([160], 300), 170)
        self.assertEqual(released_ceiling(list(range(160, 270, 10)), 300), 270)
        self.assertEqual(released_ceiling([160, 290], 300), 300)
        self.assertEqual(released_ceiling([290], 300), 300)  # clamped at the released max

    def test_grants_raise_the_limit_only_up_to_the_ceiling(self):
        self.assertEqual(cap_target(0, 160, 300), INITIAL_RANK_LIMIT)
        self.assertEqual(cap_target(110, 160, 300), 160)
        # Banked grants are held, not lost: they pay off when the gate is released.
        self.assertEqual(cap_target(130, 160, 300), 160)
        self.assertEqual(cap_target(130, 170, 300), 170)
        self.assertEqual(cap_target(250, 300, 300), 300)
        self.assertEqual(cap_target(999, 300, 300), 300)

    def test_the_recovered_account_and_the_live_save_both_reconcile(self):
        # Official save: 11 released gates (160..260) => ceiling 270, 219 grants, limit 269.
        self.assertEqual(
            cap_target(219, released_ceiling(list(range(160, 270, 10)), 300), 300), 269
        )
        # The live save after releasing the gate at 270: 220 grants, ceiling 280, limit 270.
        self.assertEqual(cap_target(220, released_ceiling(list(range(160, 280, 10)), 300), 300), 270)

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
