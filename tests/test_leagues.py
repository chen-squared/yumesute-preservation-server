"""演劇リーグ / 演劇リーグ・トリプルキャスト: the generated ladder and the official rules on it.

The recovered master's ladder stops with the service - league window 165 and triple cast
window 3037 both close on 2026-09-29 - so the server continues the chain itself. Two
things have to hold, and both are pinned here.

The calendar has to keep the cadence the archive actually used. Measured over all 202
archived rows: every window opens at 14:00 JST and ``counting_start_at`` is always
09:00 JST, 29 hours before the close in 183 of the 202 rows; league repeats every 7 days
(162 of 164 gaps, on a Tuesday) and triple cast every 14. The generated chain re-uses
that phase rather than inventing one, continues the official numbering without
collision, and is identical on every call - entities reference the window id, so a
restart may not re-draw the ladder. Only the id and the calendar are ours: each song is
taken from that ladder's own song pool together with a bonus-character set and a
per-play reward package that an archived window really used with it.

The rules applied on top are the official ones. The class ladder moves on group rank
alone (``up_user_amount`` / ``keep_user_amount`` / ``is_force_down_not_playing_league``),
and the reward bands come from master - both band tables store their two rank columns
inverted from their names (``min_rank 30, max_rank 11`` means ranks 11..30). The
recovery's own ``triple_cast_history`` row depends on that: window 3036, group rank 20,
all-class rank 1702, demotion, ``isSendedReward`` false - the payout the official server
never got to send.

The one invented part, the 99 generated opponents, is checked against its own promise:
the score curve has to be steep enough for the official cut lines to mean something, and
the parties shown for rivals may only contain cards that exist in master.
"""
import collections
import datetime as dt
import msgpack
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "vendor/server-of-dreams"))

from helpers.cache import cache, load_master_data
from helpers.mastermemory import pack, unpack
from models import master_data

load_master_data()

import league_schedule as schedule
import leagues

# The app extends its own master cache at startup (and the master download carries the
# same rows); the tests need that too, since master() only ever sees the cache.
schedule.extend_cache()


def jst(seconds):
    return dt.datetime.fromtimestamp(seconds, schedule.JST)


def ladder_classes(group_id):
    group = next(g for g in cache.league_class_group_master if g.id_ == group_id)
    return {int(c.class_type): c for c in group.classes}


class ArchivedCadence(unittest.TestCase):
    """What the archive really did - the numbers the generated chain copies."""

    def test_counting_always_starts_at_nine_in_the_morning_jst(self):
        for kind in ("league", "triple_cast"):
            hours = {jst(w.counting_start).hour for w in schedule.official(kind)}
            self.assertEqual(hours, {9}, f"{kind}: counting_start_at is not always 09:00 JST")

    def test_the_twenty_nine_hour_counting_tail_is_the_normal_one(self):
        for kind in ("league", "triple_cast"):
            leads = collections.Counter(
                w.display_end - w.counting_start for w in schedule.official(kind))
            self.assertEqual(leads.most_common(1)[0][0], schedule.COUNTING_LEAD,
                             f"{kind}: 29h is not the archived counting tail")
            self.assertGreater(leads[schedule.COUNTING_LEAD] / sum(leads.values()), 0.5)

    def test_windows_open_in_the_afternoon_at_the_measured_period(self):
        for kind, step in (("league", 7 * 86400), ("triple_cast", 14 * 86400)):
            rows = schedule.official(kind)
            hours = collections.Counter(jst(w.display_start).hour for w in rows)
            self.assertEqual(hours.most_common(1)[0][0], 14, f"{kind}: no 14:00 JST opening phase")
            self.assertGreater(hours[14] / len(rows), 0.9)
            gaps = collections.Counter(
                (b.display_start - a.display_start) // 86400 for a, b in zip(rows, rows[1:]))
            self.assertEqual(gaps.most_common(1)[0][0], step // 86400,
                             f"{kind}: the modal period is not {step // 86400} days")
        openings = collections.Counter(jst(w.display_start).strftime("%a")
                                       for w in schedule.official("league"))
        self.assertEqual(openings.most_common(1)[0][0], "Tue")


class GeneratedCalendar(unittest.TestCase):
    """The chain continues the archive instead of restarting beside it."""

    def test_the_chain_keeps_the_archived_phase_and_period(self):
        for kind, step in (("league", 7 * 86400), ("triple_cast", 14 * 86400)):
            chain = schedule.generated(kind)
            self.assertTrue(chain)
            for window in chain:
                self.assertEqual(jst(window.display_start).hour, 14)
                self.assertEqual(jst(window.counting_start).hour, 9)
                self.assertEqual(window.counting_start, window.display_end - schedule.COUNTING_LEAD)
                self.assertEqual(window.display_end - window.display_start, step)
                self.assertEqual(jst(window.display_start).strftime("%a"), "Tue")
                self.assertEqual(window.season, schedule.anchor(kind).season)
                self.assertEqual(window.class_group, schedule.anchor(kind).class_group)
            self.assertTrue(all(b.display_start == a.display_end for a, b in zip(chain, chain[1:])),
                            "the generated chain has gaps")

    def test_no_generated_window_overlaps_the_archive(self):
        for kind in ("league", "triple_cast"):
            for window in schedule.official(kind):
                for other in schedule.generated(kind):
                    self.assertTrue(
                        window.display_end <= other.display_start
                        or other.display_end <= window.display_start,
                        f"archived window {window.id} overlaps generated {other.id}")

    def test_ids_continue_the_official_numbering_without_collision(self):
        for kind in ("league", "triple_cast"):
            anchor = schedule.anchor(kind)
            generated_ids = [w.id for w in schedule.generated(kind)]
            self.assertEqual(generated_ids,
                             list(range(anchor.id + 1, anchor.id + 1 + len(generated_ids))))
            self.assertFalse({w.id for w in schedule.official(kind)} & set(generated_ids))

    def test_the_chain_is_the_same_on_every_call(self):
        # Entities reference the window id, so a restart must not re-draw the ladder.
        for kind in ("league", "triple_cast"):
            now = schedule.anchor(kind).display_end + 30 * 86400
            self.assertEqual(schedule.generated(kind, now), schedule.generated(kind, now))

    def test_the_archive_may_run_over_its_phase_but_only_by_one_period(self):
        # The archived ladder's last row overran its week (league 165 closed 23:00 JST),
        # so the chain resumes on the next phase-aligned slot. That seam has to stay
        # shorter than one period, or a generated window would sit unpublished.
        for kind, step in (("league", 7 * 86400), ("triple_cast", 14 * 86400)):
            seam = schedule.generated(kind)[0].display_start - schedule.anchor(kind).display_end
            self.assertGreaterEqual(seam, 0)
            self.assertLess(seam, step)

    def test_exactly_one_window_is_live_across_the_generated_chain(self):
        for kind, step in (("league", 7 * 86400), ("triple_cast", 14 * 86400)):
            chain = schedule.generated(kind)
            for position, window in enumerate(chain):
                for offset in (0, 60, 3600, step // 2):
                    now = window.display_start + offset
                    live = [w for w in schedule.published(kind, now)
                            if w.display_start <= now < w.display_end]
                    self.assertEqual(len(live), 1, f"{kind} window {window.id}: {len(live)} live")
                    self.assertEqual(live[0].id, window.id)
            self.assertIsNotNone(schedule.live(kind, schedule._now() + 7 * 86400))
            for ahead in (0, 30 * 86400, 365 * 86400):
                now = schedule.anchor(kind).display_end + ahead
                published = schedule.published(kind, now)
                # Every slot up to the horizon is published, so the last one closes at or
                # after it: the ladder can never run out of windows without a restart.
                self.assertGreaterEqual(max(w.display_end for w in published),
                                        now + schedule.AHEAD_DAYS * 86400,
                                        f"{kind}: the chain is not published to the horizon")

    def test_generated_windows_drop_out_but_archived_ones_stay_known(self):
        # History rows reference archived window ids for good; a generated window only
        # has to stay published long enough for its reward to be paid.
        archived = schedule.anchor("league")
        for later in (30 * 86400, schedule.PUBLISHED_GRACE * 3):
            self.assertIsNotNone(schedule.find("league", archived.id, now=archived.display_end + later))
        fresh = schedule.generated("league")[0]
        self.assertIsNotNone(schedule.find("league", fresh.id, now=fresh.display_end + 86400))
        self.assertIsNone(schedule.find("league", fresh.id,
                                        now=fresh.display_end + 2 * schedule.PUBLISHED_GRACE))

    def test_phases_run_in_the_official_order(self):
        window = schedule.anchor("league")
        self.assertEqual(window.phase(window.display_start - 1), "upcoming")
        self.assertEqual(window.phase(window.display_start), "display")
        self.assertEqual(window.phase(window.counting_start), "counting")
        self.assertEqual(window.phase(window.display_end), "closed")


class GeneratedContent(unittest.TestCase):
    """Only the id and the calendar are ours; the content is copied from master."""

    def test_songs_cycle_through_the_ladders_own_pool_without_repeats(self):
        for kind in ("league", "triple_cast"):
            pool = set(schedule.songs(kind))
            chain = schedule.generated(kind, now=schedule.anchor(kind).display_end + 400 * 86400)
            cycle = len(pool)
            self.assertGreater(len(chain), cycle)
            for start in range(0, len(chain) - cycle + 1, cycle):
                block = [w.music for w in chain[start:start + cycle]]
                self.assertEqual(len(block), cycle)
                self.assertEqual(set(block), pool, "a cycle is not one copy of the song pool")

    def test_bonus_characters_and_vocal_are_a_pairing_the_archive_used(self):
        for kind in ("league", "triple_cast"):
            used = schedule.songs(kind)
            archived_ids = {w.id for w in schedule.official(kind)}
            for window in schedule.generated(kind, now=schedule.anchor(kind).display_end + 200 * 86400):
                self.assertIn((window.source, window.sense_ids, window.vocal_version),
                              used[window.music],
                              f"window {window.id} pairs song {window.music} with bonus set "
                              f"{window.sense_ids}, which no archived window used")
                self.assertIn(window.source, archived_ids)

    def test_generated_rows_validate_against_the_master_models(self):
        for kind in ("league", "triple_cast"):
            model = getattr(master_data, schedule.KINDS[kind]["model"])
            for row in schedule.json_rows(kind):
                parsed = model.model_validate(row)
                self.assertGreater(parsed.id_, 0)
                self.assertEqual(parsed.music_master_id, row["music_master_id"])
                self.assertEqual(parsed.max_difficulty, row["max_difficulty"])
                self.assertEqual(parsed.league_class_group_master_id,
                                 row["league_class_group_master_id"])
                for field in schedule.KINDS[kind]["sense"]:
                    self.assertGreater(row[field], 0)

    def test_play_rewards_are_copied_from_the_source_window(self):
        packages = {row["id"]: row["rewards"]
                    for row in leagues.raw_master("LeaguePlayRewardPackageMaster")}
        for kind in ("league", "triple_cast"):
            rows = {row["id"]: row["rewards"] for row in schedule.play_reward_rows(kind)}
            for window in schedule.generated(kind):
                self.assertIn(window.id, rows, f"window {window.id} has no play reward row")
                self.assertEqual(rows[window.id], packages[window.source])

    def test_injected_rows_survive_the_master_download_round_trip(self):
        tables = unpack((ROOT / "private/upstream/master-original.db").read_bytes())
        before = {name: len(rows) for name, rows in tables.items()}
        changes = []
        schedule.extend_tables(tables, changes)
        self.assertTrue(changes)
        rebuilt = unpack(pack(tables))
        names = ("LeagueMaster", "TripleCastMaster", "LeaguePlayRewardPackageMaster")
        for name in names:
            self.assertGreater(len(rebuilt[name]), before[name], f"{name} gained no rows")
        for name, ident, _what in changes:
            row = next((r for r in rebuilt[name] if r[0] == ident), None)
            self.assertIsNotNone(row, f"{name} {ident} lost in the round trip")
            self.assertIsInstance(row, list)
        league = next(r for r in rebuilt["LeagueMaster"]
                      if r[0] == schedule.anchor("league").id + 1)
        stamps = [value for value in league if isinstance(value, msgpack.Timestamp)]
        self.assertEqual(len(stamps), 3)
        self.assertEqual(stamps[2].seconds - stamps[0].seconds, 7 * 86400)
        self.assertEqual(stamps[1].seconds, stamps[2].seconds - schedule.COUNTING_LEAD)


class ClassLadder(unittest.TestCase):
    """LeagueClassGroupMaster's rule, applied without padding."""

    def setUp(self):
        self.classes = ladder_classes(schedule.anchor("league").class_group)

    def test_promotion_and_relegation_follow_group_rank_alone(self):
        cast = self.classes[3]  # up 60, keep 30, force-down when never played
        self.assertEqual(leagues.change_for(cast, 60, True), leagues.int_change("Up"))
        self.assertEqual(leagues.change_for(cast, 61, True), leagues.int_change("Keep"))
        self.assertEqual(leagues.change_for(cast, 90, True), leagues.int_change("Keep"))
        self.assertEqual(leagues.change_for(cast, 91, True), leagues.int_change("Down"))
        self.assertEqual(leagues.change_for(cast, 99, False), leagues.int_change("Down"),
                         "a class never played in must drop")
        rookie = self.classes[1]  # up_user_amount is null: nothing above Rookie
        self.assertEqual(leagues.change_for(rookie, 1, True), leagues.int_change("Keep"))
        self.assertEqual(leagues.change_for(rookie, 500, False), leagues.int_change("Keep"),
                         "Rookie has neither a keep band nor a force-down")
        dai_star = self.classes[7]  # up_user_amount 0: the ladder tops out here
        self.assertEqual(leagues.change_for(dai_star, 1, True), leagues.int_change("Keep"))

    def test_class_moves_stay_inside_the_ladder(self):
        self.assertEqual(leagues.moved(3, leagues.int_change("Up")), 4)
        self.assertEqual(leagues.moved(3, leagues.int_change("Keep")), 3)
        self.assertEqual(leagues.moved(3, leagues.int_change("Down")), 2)
        self.assertEqual(leagues.moved(7, leagues.int_change("Up")), 7)
        self.assertEqual(leagues.moved(1, leagues.int_change("Down")), 1)

    def test_both_ladders_use_the_same_seven_classes_and_cut_lines(self):
        self.assertEqual(leagues.CLASSES, [1, 2, 3, 4, 5, 6, 7])
        for group in (3, 200):
            classes = ladder_classes(group)
            self.assertEqual(sorted(classes), leagues.CLASSES)
            self.assertEqual([classes[c].up_user_amount for c in leagues.CLASSES],
                             [None, 80, 60, 40, 10, 5, 0])
            self.assertEqual([bool(classes[c].is_force_down_not_playing_league)
                              for c in leagues.CLASSES],
                             [False, False, True, True, True, True, True])

    def test_a_window_the_official_server_graded_keeps_its_numbers(self):
        # The recovered save, window 3036: graded and demoted, reward never sent.
        row = {"tripleCastMasterId": 3036, "classType": 3, "historyCount": 0,
               "isSendedReward": False, "isPlayed": False, "classChangeType": 3,
               "groupRank": 20, "globalRank": 113, "allClassGlobalRank": 1702}
        outcome = leagues.recorded_outcome(row)
        self.assertEqual((outcome["group_rank"], outcome["global_rank"],
                          outcome["all_class_global_rank"]), (20, 113, 1702))
        self.assertEqual(outcome["change"], leagues.int_change("Down"))
        self.assertTrue(outcome["official"])
        # A window we run ourselves starts ungraded, so it is recomputed instead.
        self.assertIsNone(leagues.recorded_outcome({**row, "groupRank": 0, "classChangeType": 0}))


class OfficialRewards(unittest.TestCase):
    """The packages the ladder pays, straight from master."""

    def test_group_rank_bands_handle_the_inverted_rank_columns(self):
        # Row 306 is stored as (min_rank 30, max_rank 11) and means ranks 11..30.
        package = leagues.group_rank_package("triple_cast", 3, 20)
        self.assertEqual(package.id_, 306)
        self.assertEqual(leagues._things(package), [(1, 130011, 520)])
        self.assertEqual(leagues.group_rank_package("triple_cast", 3, 11).id_, 306)
        self.assertEqual(leagues.group_rank_package("triple_cast", 3, 30).id_, 306)
        self.assertEqual(leagues.group_rank_package("triple_cast", 3, 31).id_, 307)
        self.assertEqual(leagues.group_rank_package("league", 3, 1).id_, 301)
        self.assertEqual(leagues._things(leagues.group_rank_package("league", 3, 1)),
                         [(1, 130011, 400)])

    def test_the_all_class_band_grants_that_windows_trophy(self):
        self.assertEqual(leagues._things(leagues.all_class_package("triple_cast", 3036, 1702)),
                         [(6, 260251, 1)])
        self.assertEqual(leagues._things(leagues.all_class_package("triple_cast", 3036, 100)),
                         [(6, 260250, 1)])
        self.assertIsNone(leagues.all_class_package("triple_cast", 3036, 0))

    def test_the_pending_payout_is_the_official_demotion_package(self):
        demoted = leagues.ladder(schedule.anchor("triple_cast"), 3)
        self.assertTrue(demoted.is_force_down_not_playing_league)
        package = leagues.master_reward_package(demoted.down_reward_package_master_id)
        self.assertEqual(package.id_, 330)
        self.assertEqual(leagues._things(package),
                         [(13, 0, 360), (1, 130042, 165), (1, 130043, 65)])
        kept = leagues.master_reward_package(demoted.keep_reward_package_master_id)
        self.assertEqual(kept.id_, 320)

    def test_every_generated_window_pays_per_play_rewards(self):
        # master() reads the cache, and another test module may have reloaded it since
        # this one was imported - the app does the same injection at startup.
        schedule.extend_cache()
        for kind in ("league", "triple_cast"):
            for window in schedule.generated(kind):
                self.assertTrue(leagues.play_reward_things(window),
                                f"window {window.id} has no per-play reward")


class FreeReplays(unittest.TestCase):
    """Both ladders are meant to be replayed, so a run is never charged stamina."""

    def test_a_ladder_run_costs_nothing_even_for_a_priced_song(self):
        priced = [m for m in cache.music_master if m.stamina_consumption][:25]
        self.assertTrue(priced, "no priced song in master to check against")
        for music in priced:
            self.assertEqual(leagues.ladder_stamina_cost(music), 0,
                             f"music {music.id_} costs {music.stamina_consumption} elsewhere")

    def test_the_archive_already_prices_the_ladder_songs_that_way(self):
        prices = {m.id_: int(m.stamina_consumption) for m in cache.music_master}
        for kind in ("league", "triple_cast"):
            rows = schedule.official(kind)
            free = sum(1 for w in rows if prices[w.music] == 0)
            if kind == "triple_cast":
                self.assertEqual(free, len(rows), "triple cast songs are not all free in master")
            else:
                self.assertGreater(free * 2, len(rows),
                                   "most league songs should already be free in master")


class GeneratedField(unittest.TestCase):
    """The invented part, held to what it claims: labelled, and fair to the ladder."""

    def test_the_curve_spans_both_sides_of_the_players_own_best(self):
        rng = random.Random("field")
        top = leagues.rival_score(1_000_000, 1, rng)[0]
        bottom = leagues.rival_score(1_000_000, leagues.RIVAL_COUNT, rng)[0]
        self.assertGreater(top, 1_200_000)
        self.assertLess(bottom, 650_000)

    def test_the_official_cut_lines_ask_for_a_real_improvement(self):
        rng = random.Random("cut")
        base = 1_000_000
        field = sorted((leagues.rival_score(base, ordinal, rng)[0]
                        for ordinal in range(1, leagues.RIVAL_COUNT + 1)), reverse=True)
        # A group is 99 rivals plus this account, so rank n of the ladder below is the
        # rival at index n-1: Elite rises in the top 40, Star in the top 5.
        self.assertGreater(field[39], base, "promotion out of Elite must beat a personal best")
        self.assertGreater(field[4], base * 1.2, "promotion into Star must be a clear win")
        self.assertLess(field[-1], base, "the field cannot all be out of reach")

    def test_rival_parties_only_use_cards_that_exist(self):
        characters = {c.id_ for c in cache.character_master}
        posters = {p.id_ for p in cache.poster_master}
        accessories = {a.id_ for a in cache.accessory_master}
        rng = random.Random("party")
        for strength in (0.0, 0.6, 1.3):
            party = leagues.rival_party(rng, strength)
            self.assertEqual(len(party), 5)
            ids = [card["character_master_id"] for card in party]
            self.assertEqual(len(set(ids)), 5, "one character cannot fill five slots")
            for card in party:
                self.assertIn(card["character_master_id"], characters)
                self.assertIn(card["poster_master_id"], posters)
                self.assertIn(card["accessory_master_id"], accessories)
                self.assertTrue(1 <= card["character_level"] <= 120)
                self.assertGreater(card["status"], 0)

    def test_generated_rivals_are_labelled_and_stay_in_their_own_table(self):
        self.assertEqual(leagues.RIVAL_LABEL, "ローカル対抗")
        # No official entity table may receive a generated rival.
        self.assertNotIn(leagues.RIVAL_TABLE, leagues.SPEC["league"].values())
        self.assertTrue(leagues.RIVAL_TABLE.startswith("preservation_"))
        for spec in leagues.SPEC.values():
            self.assertTrue(spec["member"].startswith(("League", "TripleCast")))


if __name__ == "__main__":
    unittest.main()
