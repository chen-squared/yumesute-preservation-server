"""The やねうら (April-Fool) alternate charts: an open entry, and the run it records.

``AnotherNotationMaster`` is the 112-chart meme mode - 30 charts from 2024, 33 from 2025, the
37 ヤネウラ charts of 2026 and 12 added 2026-07-26 - and all of them are archived with
``end_date`` 2100, with their chart files here (``Notations/20001..20112``, none missing). The
mode was hidden by one row: ``EventMaster`` 21018, the only ``event_type`` 23 row with
``is_aside_live_button`` and ``aside_live_button_type`` 3 (the side button on the live screen),
closed on 2026-04-08. ``preservation_dates`` re-dates that one row by name; this test pins that
it is exactly that row and nothing else, and that the chart rows themselves were never touched.

The other half is ``Lives/FinishAnotherNotationLive``, a stub in the pinned vendor: this test
pins the record it writes - graded by the same rules as a normal live (``live_finish``), a
best-of per chart, nothing banked by an auto-play run - which run it settles itself and which
it hands to the normal finish (``decide``), and the ``LiveMaster`` row an alternate
chart is played on, which is also what lets the archived ヤネウラ狂奏曲 course
(``MusicCourseMaster`` 85, whose three details name meme charts and no song) start at all.

Reads no database. The routes on a real save are checked by ``check_alternate_notations.py``.
"""
import asyncio
import collections
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "vendor/server-of-dreams"))

import msgpack
from helpers.cache import cache, load_master_data
from helpers.live_result import play_totals
from helpers.mastermemory import unpack
from helpers.score import verify_score_blocks
from models import BaseScoreBlock, FinishAnotherNotationLivePayload
from models.enums import AchievementRateGrades, ClearLamps, EventTypes, TimingTypes
from models.keys import KEYS

load_master_data()

import alternate_notations as notations
import music_courses as courses
import preservation_dates as dates

ENTRY = 21018                       # the ヤネウラ entry: the only alt event with a live button
EARLIER = (21003, 21010)            # the 2024 / 2025 editions: same type, no button
MEME_COURSE = 85                    # ヤネウラ狂奏曲, MusicCourseType.Special
ASSETS = ROOT / "vendor/server-of-dreams/_data/assets/Notations"
BLOCK_FIELDS = ("score", "life", "note_id", "timing_type", "combo")


def original():
    """The archived master, before any preservation change."""
    return unpack((ROOT / "private/upstream/master-original.db").read_bytes())


def served():
    """The master the server actually hands the client."""
    return unpack(dates.preserved_master()[0])


def columns(table):
    return {field: key for key, field, *_ in KEYS[table]}


def chained(blocks):
    """Score blocks whose hash chain the server's verifier accepts."""
    running = 0
    for block in blocks:
        running += sum(int(getattr(block, name)) for name in BLOCK_FIELDS)
        block.hash = running
    return blocks


def run(notes=40, timing=TimingTypes.PERFECT_STAR, life=1, auto=False, notation_id=65):
    """A finished alternate-chart run: ``notes`` judged notes, all of one timing."""
    return FinishAnotherNotationLivePayload(
        another_notation_master_id=notation_id,
        is_auto_play=auto,
        base_score_blocks=chained([
            BaseScoreBlock(score=101, life=life, note_id=i, timing_type=timing, combo=i + 1)
            for i in range(notes)]))


class Entry(unittest.TestCase):
    """The April-Fool entry row is held open; its charts always were."""

    def test_the_entry_event_is_held_open(self):
        archive, now = original(), served()
        fields = columns("EventMaster")
        was = {row[0]: row for row in archive["EventMaster"]}[ENTRY]
        row = {row[0]: row for row in now["EventMaster"]}[ENTRY]
        self.assertEqual(int(row[fields["event_type"]]), int(EventTypes.AnotherNotation))
        self.assertTrue(row[fields["is_aside_live_button"]])
        self.assertEqual(int(row[fields["aside_live_button_type"]]), 3)
        for field in ("end_date", "force_end_date"):
            self.assertEqual(row[fields[field]], msgpack.Timestamp(dates.END, 0), field)
            self.assertNotEqual(was[fields[field]], msgpack.Timestamp(dates.END, 0))
        self.assertEqual(row[fields["start_date"]], was[fields["start_date"]],
                         "the window's start is official and stays where it is")

    def test_only_the_named_event_is_reopened(self):
        archive, now = original(), served()
        fields = columns("EventMaster")
        changed = {(name, ident) for name, ident, _ in dates.preserved_master()[2]
                   if name == "EventMaster"}
        alt = {row[0] for row in archive["EventMaster"]
               if int(row[fields["event_type"]]) == int(EventTypes.AnotherNotation)}
        self.assertIn(("EventMaster", ENTRY), changed)
        self.assertTrue(alt, "the master has an event row per edition of the mode")
        for ident in EARLIER:
            self.assertEqual({row[0]: row for row in now["EventMaster"]}[ident],
                             {row[0]: row for row in archive["EventMaster"]}[ident],
                             f"{ident} is a finished edition and stays closed")

    def test_the_charts_are_permanent_and_present(self):
        now = served()
        rows = now["AnotherNotationMaster"]
        fields = columns("AnotherNotationMaster")
        self.assertEqual(len(rows), 112)
        self.assertEqual({row[fields["end_date"]].seconds for row in rows}, {dates.END},
                         "every archived meme chart runs to the permanent sentinel date")
        # the editions open in staggered stages during their day, so count them by day
        editions = collections.Counter(row[fields["start_date"]].seconds // 86400 for row in rows)
        self.assertEqual(sorted(editions.values()), [12, 30, 33, 37],
                         "four editions: 2024, 2025, the 2026 ヤネウラ one, 12 regular charts")
        missing = []
        for row in rows:
            folder = ASSETS / str(row[fields["notation_path"]])
            for name in (f"{row[fields['difficulty']]}.enc", "music_config.enc"):
                if not (folder / name).is_file():
                    missing.append((row[0], name))
        self.assertEqual(missing, [], "the chart files have to be here for the mode to play")

    def test_every_chart_is_an_arrangement_of_an_archived_song(self):
        songs = {music.id_ for music in cache.music_master}
        self.assertTrue(all(int(row.music_master_id) in songs
                            for row in cache.another_notation_master))
        # AnotherNotationTypes: Shelved 1, Defect 2, Change 3, Mysterious 4, Madness 5,
        # Reproduction 6, Another 7, Easy 8 - the archive never uses 6.
        self.assertEqual({int(row.another_notation_type) for row in cache.another_notation_master},
                         {1, 2, 3, 4, 5, 7, 8})


class Charts(unittest.TestCase):
    """Which ``LiveMaster`` row an alternate chart is played on."""

    def test_every_alternate_chart_has_its_own_live_row(self):
        charts = {row.id_: row for row in cache.live_master}
        self.assertTrue(all(row.id_ == row.music_master_id * 100 + row.difficulty
                            for row in cache.live_master),
                        "the archived id rule the client's own lookup depends on")
        for alt in cache.another_notation_master:
            chart = notations.chart_for(alt)
            self.assertIsNotNone(chart, f"alternate chart {alt.id_}")
            self.assertEqual(int(chart.music_master_id), int(alt.music_master_id))
            self.assertEqual(int(chart.difficulty), int(alt.difficulty))
            self.assertEqual(int(chart.id_), int(alt.music_master_id) * 100 + int(alt.difficulty))
            self.assertIn(int(chart.id_), charts)

    def test_the_levels_differ_which_is_the_point(self):
        """A meme chart is not its song's own chart, so it may not write that song's record."""
        alt = next(row for row in cache.another_notation_master if row.id_ == 65)
        chart = notations.chart_for(alt)
        self.assertEqual(int(alt.level), 107)
        self.assertEqual(int(chart.level), 28)
        self.assertEqual(int(alt.difficulty), int(chart.difficulty))

    def test_an_unknown_alternate_chart_resolves_to_nothing(self):
        self.assertIsNone(notations.notation(0))
        self.assertIsNone(notations.notation(99999))
        self.assertIsNone(notations.chart_for(None))


class Record(unittest.TestCase):
    """What one alternate-chart run banks, under the normal live's own rules."""

    def test_a_first_clean_run_writes_the_archived_shape(self):
        values, figures = notations.record_update(None, run(), True)
        self.assertEqual(values, {"clearLamp": int(ClearLamps.AllPerfect),
                                  "rateGrade": int(AchievementRateGrades.SSS),
                                  "achievementRatePercentRecord": "101.0000"})
        self.assertEqual((figures["rate"], figures["lamp"], figures["prev_rate"]),
                         (101.0, int(ClearLamps.AllPerfect), 0.0))
        self.assertTrue(figures["high"])

    def test_a_better_run_replaces_the_record(self):
        held = {"clearLamp": int(ClearLamps.Clear), "rateGrade": int(AchievementRateGrades.S),
                "achievementRatePercentRecord": "98.3734"}
        values, figures = notations.record_update(held, run(timing=TimingTypes.PERFECT), True)
        self.assertEqual(values["achievementRatePercentRecord"], "100.0000")
        self.assertEqual(values["clearLamp"], int(ClearLamps.AllPerfect))
        self.assertEqual(figures["prev_rate"], 98.3734)
        self.assertTrue(figures["high"])

    def test_a_worse_run_leaves_the_record_alone(self):
        held = {"clearLamp": int(ClearLamps.AllPerfect), "rateGrade": int(AchievementRateGrades.SSS),
                "achievementRatePercentRecord": "101.0000"}
        values, figures = notations.record_update(held, run(timing=TimingTypes.GREAT), True)
        self.assertEqual(values["achievementRatePercentRecord"], "101.0000")
        self.assertEqual(values["clearLamp"], int(ClearLamps.AllPerfect))
        self.assertFalse(figures["high"])
        self.assertEqual(values["rateGrade"], int(AchievementRateGrades.SSS))

    def test_a_failed_run_never_moves_the_lamp_but_keeps_its_rate(self):
        payload = run(timing=TimingTypes.PERFECT, life=0)   # last block's life is 0
        self.assertFalse(play_totals(payload)[1])
        held = {"clearLamp": int(ClearLamps.AllPerfect), "rateGrade": int(AchievementRateGrades.S),
                "achievementRatePercentRecord": "87.9386"}
        values, figures = notations.record_update(held, payload, False)
        self.assertEqual(figures["lamp"], int(ClearLamps.None_))
        self.assertEqual(values["clearLamp"], int(ClearLamps.AllPerfect))
        self.assertEqual(values["achievementRatePercentRecord"], "100.0000")

    def test_auto_play_banks_nothing(self):
        held = {"clearLamp": int(ClearLamps.Clear), "rateGrade": int(AchievementRateGrades.APlus),
                "achievementRatePercentRecord": "99.7689"}
        values, figures = notations.record_update(held, run(auto=True), True)
        self.assertEqual(values["achievementRatePercentRecord"], "99.7689")
        self.assertEqual(values["clearLamp"], int(ClearLamps.Clear))
        self.assertEqual(values["rateGrade"], int(AchievementRateGrades.APlus))
        self.assertFalse(figures["high"])

    def test_the_blocks_survive_the_route_handover(self):
        """A run reported on this route can settle on the normal one unchanged."""
        payload = run(notes=25)
        self.assertTrue(verify_score_blocks(payload))
        moved = notations.as_live_payload(payload)
        self.assertTrue(verify_score_blocks(moved))
        self.assertEqual(moved.base_score_blocks, payload.base_score_blocks)
        self.assertEqual(play_totals(moved), play_totals(payload))

    def test_a_tampered_run_fails_the_same_verifier(self):
        payload = run(notes=25)
        payload.base_score_blocks[10].score += 100_000
        self.assertFalse(verify_score_blocks(payload))


class Route(unittest.TestCase):
    """Which run ``Lives/FinishAnotherNotationLive`` settles itself.

    Measured on the device: nine alternate-chart plays posted this route and never
    ``Lives/Start`` (``logs/backend.log``) - the mode opens from the live screen's side button
    and reports a result without a live session, so a missing one is the normal case here and
    not a reason to refuse the run. Refusing it is what made every settle screen read 0.0000%.
    """

    def test_a_meme_run_is_recorded_with_or_without_a_live_session(self):
        self.assertEqual(notations.decide('missing'), notations.RECORD)
        self.assertEqual(notations.decide('normal'), notations.RECORD)

    def test_a_course_stage_settles_where_its_life_pool_is(self):
        self.assertEqual(notations.decide('course'), notations.HANDOFF)

    def test_another_mode_run_is_refused(self):
        for mode in ('league', 'triple_cast', 'concert', 'audition', 'lesson'):
            self.assertEqual(notations.decide(mode), notations.REFUSE, mode)


class MemeCourse(unittest.TestCase):
    """ヤネウラ狂奏曲: the permanent special course whose three details are meme charts."""

    def setUp(self):
        self.course = next(row for row in cache.music_course_master if row.id_ == MEME_COURSE)
        self.details = sorted(self.course.details, key=lambda d: d.set_list_number)

    def test_the_archived_course_names_meme_charts_not_songs(self):
        self.assertEqual(self.course.name, "ヤネウラ狂奏曲")
        self.assertEqual(int(self.course.music_course_type), 4)      # MusicCourseType.Special
        self.assertIsNone(self.course.end_date)                      # permanent
        self.assertEqual(len(self.details), 3)
        self.assertTrue(all(d.live_master_id is None and d.another_notation_master_id
                            for d in self.details))
        self.assertEqual([int(d.another_notation_master_id) for d in self.details], [30, 100, 58])

    def test_its_charts_are_accepted_and_other_songs_are_not(self):
        def allowed(detail, live_id):
            return asyncio.run(courses.chart_matches(None, self.course, detail, live_id, 0))

        for detail in self.details:
            chart = courses.notation_chart(detail)
            self.assertIsNotNone(chart, f"detail {detail.id_}")
            self.assertTrue(allowed(detail, chart.id_))
            same_song = [row.id_ for row in cache.live_master
                         if int(row.music_master_id) == int(chart.music_master_id)]
            self.assertFalse(allowed(detail, next(i for i in same_song if i != chart.id_)),
                             "the detail names one chart, not any arrangement of the song")
            other = next(row.id_ for row in cache.live_master
                         if int(row.music_master_id) != int(chart.music_master_id))
            self.assertFalse(allowed(detail, other))

    def test_its_set_list_is_fixed_so_the_draw_route_still_answers(self):
        drawn = courses.draw(self.course)
        self.assertEqual([number for number, _ in drawn], [1, 2, 3])
        self.assertEqual([live for _, live in drawn],
                         [int(courses.notation_chart(d).id_) for d in self.details])
        self.assertEqual(drawn, courses.draw(self.course), "nothing is drawn for a fixed detail")


if __name__ == "__main__":
    unittest.main()
