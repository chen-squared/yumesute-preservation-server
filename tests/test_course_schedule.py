"""The weekly music course: the generated editions, and the ranking paid on them.

The recovered master's weekly chain stops with the service - the last edition is 第79回
(ids 356/357) and its window closed 2026-09-21 - so the course screen would be left with the
permanent 段位認定 courses only. ``course_schedule`` continues that chain and
``music_courses`` ranks the attempts on it; both halves are pinned here.

The calendar is measured over all 158 archived 「第N回 ウィークリーコース」 rows: 79 editions
of two rows each, one 【STELLA部門】 and one 【OLIVIER部門】, 7 days apart, and the 156 regular
ones all run Monday 17:00 -> Monday 00:00 in the master's wall clock (151 hours). The
generated chain continues that phase and numbering, changes nothing else about the row, and
is identical on every call - account rows reference the course id, so a restart may not
re-draw the week.

The songs are the deliberate wild card: three different charts from the department's own
archived pool, dealt at random per edition. The department rule is measured too (STELLA
difficulty 4 at levels 23..29, OLIVIER difficulty 5 at levels 101..108), and every chart is
one an archived weekly course already used.

The rewards are the official ones. The weekly courses have no MusicCourseRewardGroupMaster
rows at all; their reward table is CourseRankingRewardMaster, seven trophy bands per course,
so a generated edition carries a copy of a real edition's bands. The only invented part is
the field the ranks are taken against, and its scale is bounded by the archive: the ranked
quantity is the three songs' total achievement rate, capped at 101.0 per note (303.0 for a
course) with this account's 13 archived records at 299.47..302.76.
"""
import collections
import datetime as dt
import json
import msgpack
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "vendor/server-of-dreams"))

from helpers.cache import cache, load_master_data
from helpers.mastermemory import pack, unpack
from models import master_data
from models.enums import TimingTypes

load_master_data()

import course_schedule as schedule
import music_courses as courses

# The app extends its own master cache at startup (and the master download carries the same
# rows); the tests need that too, since master() only ever sees the cache.
schedule.extend_cache()

STELLA, OLIVIER = "STELLA部門", "OLIVIER部門"


def jst(seconds):
    """Wall clock of a master timestamp, which carries JST hours behind a bogus +00:00."""
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc)


def archived_weekly():
    return schedule.archived()


def archived_band_rows():
    """The rank-band rows the archive shipped - read from source, since the cache carries
    the generated ones too."""
    path = ROOT / "vendor/server-of-dreams/_data/masterdata/CourseRankingRewardMaster.json"
    return json.loads(path.read_text(encoding="utf-8"))


def archived_bands():
    """course id -> [(low rank, high rank, trophy id)] in band order."""
    out = collections.defaultdict(list)
    for row in archived_band_rows():
        out[int(row["music_course_master_id"])].append(
            (tuple(sorted((int(row["target_highest_rank"]), int(row["target_lowest_rank"])))),
             [int(thing["thing_id"]) for thing in row["rewards"] or []]))
    return {course: sorted(entries) for course, entries in out.items()}


def lives_of(row):
    return [detail["live_master_id"] for detail in row["details"]]


class ArchivedCadence(unittest.TestCase):
    """What the archive actually did, so the generated chain can be checked against it."""

    def test_the_chain_is_seventy_nine_editions_of_two_rows(self):
        rows = archived_weekly()
        self.assertEqual(len(rows), 158)
        editions = schedule.editions()
        self.assertEqual(len(editions), 79)
        self.assertEqual([number for number, _ in editions], list(range(1, 80)))
        for _, pair in editions:
            self.assertEqual({schedule.department(row["name"]) for row in pair},
                             {STELLA, OLIVIER})
            self.assertEqual([row["id"] for row in pair], sorted(row["id"] for row in pair))

    def test_regular_windows_run_monday_afternoon_to_monday_midnight(self):
        hours = collections.Counter((jst(schedule._seconds(row["start_date"])).strftime("%a %H:%M"),
                                     jst(schedule._seconds(row["end_date"])).strftime("%a %H:%M"))
                                    for row in archived_weekly())
        self.assertEqual(hours.most_common(1)[0][0], ("Mon 17:00", "Mon 00:00"))
        self.assertGreater(hours[("Mon 17:00", "Mon 00:00")], 150)
        durations = collections.Counter(
            schedule._seconds(row["end_date"]) - schedule._seconds(row["start_date"])
            for row in archived_weekly())
        self.assertEqual(durations.most_common(1)[0][0], schedule.WINDOW)

    def test_the_editions_are_seven_days_apart(self):
        starts = [schedule._seconds(row["start_date"]) for _, pair in schedule.editions()
                  for row in pair[:1]]
        gaps = collections.Counter(round((b - a) / schedule.STEP, 6)
                                   for a, b in zip(starts, starts[1:]))
        self.assertEqual(gaps.most_common(1)[0], (1.0, len(starts) - 2))

    def test_each_department_plays_exactly_one_difficulty_tier(self):
        charts = collections.defaultdict(set)
        live = {row.id_: row for row in cache.live_master}
        for row in archived_weekly():
            for detail in row["details"]:
                chart = live[detail["live_master_id"]]
                charts[schedule.department(row["name"])].add(
                    (int(chart.difficulty), int(chart.level)))
        self.assertEqual({difficulty for difficulty, _ in charts[STELLA]}, {4})
        self.assertEqual({difficulty for difficulty, _ in charts[OLIVIER]}, {5})
        self.assertEqual((min(level for _, level in charts[STELLA]),
                          max(level for _, level in charts[STELLA])), (23, 29))
        self.assertEqual((min(level for _, level in charts[OLIVIER]),
                          max(level for _, level in charts[OLIVIER])), (101, 108))

    def test_the_weekly_courses_are_the_ranked_courses(self):
        ranked = {row.music_course_master_id for row in cache.course_ranking_reward_master}
        weekly = {row["id"] for row in archived_weekly()}
        self.assertTrue(weekly <= ranked, sorted(weekly - ranked))
        for course in cache.music_course_master:
            if course.id_ in weekly:
                self.assertEqual(int(course.music_course_type), schedule.COURSE_TYPE)
                self.assertEqual(int(course.initial_life), 300)
                self.assertEqual(course.required_item_master_id, 130201)
                self.assertEqual(int(course.required_amount), 1)
        # Their reward table is the rank bands and nothing else.
        groups = {row.music_course_master_id for row in cache.music_course_reward_group_master}
        self.assertFalse(weekly & groups)


class GeneratedEditions(unittest.TestCase):
    def test_the_chain_picks_up_one_period_after_the_archive(self):
        number, templates = schedule.anchor()
        self.assertEqual(number, 79)
        last_start = max(schedule._seconds(row["start_date"]) for row in templates)
        first = schedule.generated()[0]
        self.assertEqual(first.start, last_start + schedule.STEP)
        self.assertEqual(first.name, f"第{number + 1}回 ウィークリーコース【{STELLA}】")

    def test_generated_rows_keep_the_measured_shape(self):
        rows = schedule.json_rows()
        self.assertTrue(rows)
        for row in rows:
            parsed = master_data.MusicCourseMaster.model_validate(row)
            self.assertEqual(int(parsed.music_course_type), schedule.COURSE_TYPE)
            self.assertEqual(parsed.initial_life, 300)
            self.assertEqual(parsed.required_item_master_id, 130201)
            self.assertEqual(parsed.required_amount, 1)
            self.assertEqual(int(parsed.unlock_condition), 0)
            start, end = schedule._seconds(row["start_date"]), schedule._seconds(row["end_date"])
            self.assertEqual(end - start, schedule.WINDOW)
            self.assertEqual((jst(start).strftime("%a %H:%M"), jst(end).strftime("%a %H:%M")),
                             ("Mon 17:00", "Mon 00:00"))
            self.assertEqual([detail.set_list_number for detail in parsed.details], [1, 2, 3])
            for detail in parsed.details:
                self.assertEqual(int(detail.vocal_version), 1)
                self.assertIsNone(detail.random_difficulty)

    def test_ids_continue_the_official_numbering_without_collision(self):
        archived_courses = {int(row["id"]) for row in schedule.archived()}
        archived_details = {int(detail["id"]) for row in schedule.archived()
                            for detail in row["details"]}
        archived_bands = {int(row["id"]) for row in archived_band_rows()}
        courses = schedule.json_rows()
        bands = schedule.ranking_reward_rows()
        details = [detail for row in courses for detail in row["details"]]
        for group, archived in ((courses, archived_courses), (details, archived_details),
                                (bands, archived_bands)):
            ids = [row["id"] for row in group]
            self.assertFalse({*ids} & archived, "a generated id collides with the archive")
            self.assertEqual(len(ids), len(set(ids)), "a generated id repeats")

    def test_the_chain_is_the_same_on_every_call(self):
        self.assertEqual(schedule.json_rows(), schedule.json_rows())
        self.assertEqual(schedule.ranking_reward_rows(), schedule.ranking_reward_rows())

    def test_two_editions_are_live_at_once_and_the_week_then_closes(self):
        rows = schedule.generated()
        for row in rows[:6]:
            open_rows = schedule.live(row.start + schedule.WINDOW // 2)
            self.assertEqual(len(open_rows), 2, f"{row.name} is not open in its own window")
            self.assertEqual({schedule.department(other.name) for other in open_rows},
                             {STELLA, OLIVIER})
        # Measured on the archive: the window closes Monday 00:00 and the next opens Monday
        # 17:00, so 17 hours of every week carry no weekly course at all.
        first = rows[0]
        self.assertEqual(schedule.live(first.end + 6 * 3600), [])
        self.assertEqual(len(schedule.live(first.end + 18 * 3600)), 2)

    def test_songs_come_from_that_departments_own_pool_and_repeat_no_song(self):
        live = {row.id_: row for row in cache.live_master}
        pools = {label: set(info["pool"]) for label, info in schedule.departments().items()}
        tiers = {STELLA: (4, (23, 29)), OLIVIER: (5, (101, 108))}
        seen = set()
        for course in schedule.generated():
            label = schedule.department(course.name)
            self.assertEqual(len(course.lives), 3)
            self.assertEqual(len({live[ident].music_master_id for ident in course.lives}), 3,
                             f"{course.name} plays the same song twice")
            for ident in course.lives:
                self.assertIn(ident, pools[label], f"{course.name} uses a foreign chart")
                self.assertNotIn(ident, schedule.UNPLAYABLE)
                difficulty, levels = tiers[label]
                self.assertEqual(int(live[ident].difficulty), difficulty)
                self.assertTrue(levels[0] <= int(live[ident].level) <= levels[1])
            seen.add(course.lives)
        self.assertGreater(len(seen), 10, "the draw is not drawing")

    def test_the_draw_is_stable_per_course(self):
        course = schedule.generated()[0]
        label = schedule.department(course.name)
        self.assertEqual(schedule.songs(course.id, label),
                         schedule.songs(course.id, label))
        other = schedule.songs(course.id + 2, label)
        self.assertNotEqual(course.lives, other)

    def test_ranking_bands_are_copied_from_a_real_edition(self):
        archived = archived_bands()
        generated = schedule.ranking_reward_rows()
        by_course = collections.defaultdict(list)
        for row in generated:
            by_course[row["music_course_master_id"]].append(row)
        trophies = {row.id_ for row in cache.trophy_master}
        for course in schedule.generated():
            rows = by_course[course.id]
            self.assertTrue(rows, f"{course.name} has no band to pay")
            self.assertEqual([tuple(sorted((row["target_highest_rank"], row["target_lowest_rank"])))
                              for row in rows],
                             [ranks for ranks, _ in archived[course.source]],
                             f"{course.name} does not repeat its source edition's bands")
            self.assertEqual([[int(thing["thing_id"]) for thing in row["rewards"]]
                              for row in rows],
                             [things for _, things in archived[course.source]])
            for row in rows:
                self.assertEqual(len(row["rewards"]), 1)
                for thing in row["rewards"]:
                    self.assertEqual(int(thing["thing_type"]), 6)
                    self.assertIn(thing["thing_id"], trophies)
                    self.assertGreater(int(thing["thing_quantity"]), 0)

    def test_the_seven_trophy_bands_are_the_standard_weekly_reward(self):
        archived = archived_bands()
        shapes = collections.Counter(tuple(ranks for ranks, _ in bands)
                                     for bands in archived.values())
        standard = ((1, 1), (2, 2), (3, 3), (4, 10), (11, 50), (51, 100), (101, 9999999))
        self.assertEqual(shapes.most_common(1)[0][0], standard)
        # Only 第1回 (200/201) had four bands; everything the chain copies from 第2回 on has
        # the seven, and 段位認定 courses have none at all - their rewards are grade-based.
        self.assertEqual(shapes[standard], len(archived) - 2)
        self.assertEqual(courses.bands(3009), [])

    def test_injected_rows_survive_the_master_download_round_trip(self):
        tables = unpack((ROOT / "private/upstream/master-original.db").read_bytes())
        before = {name: len(rows) for name, rows in tables.items()}
        changes = []
        schedule.extend_tables(tables, changes)
        self.assertTrue(changes)
        rebuilt = unpack(pack(tables))
        for name in ("MusicCourseMaster", "CourseRankingRewardMaster"):
            self.assertGreater(len(rebuilt[name]), before[name], f"{name} gained no rows")
        first = schedule.generated()[0]
        row = next(r for r in rebuilt["MusicCourseMaster"] if r[0] == first.id)
        stamps = [value for value in row if isinstance(value, msgpack.Timestamp)]
        self.assertEqual(len(stamps), 2)
        self.assertEqual(stamps[0].seconds, first.start)
        self.assertEqual(stamps[1].seconds - stamps[0].seconds, schedule.WINDOW)
        self.assertEqual([detail[0] for detail in row[12]],
                         [first.id * 100 + n for n in (1, 2, 3)])
        self.assertEqual([detail[2] for detail in row[12]], list(first.lives))
        self.assertTrue([r for r in rebuilt["CourseRankingRewardMaster"]
                         if r[1] == first.id])

    def test_extend_cache_adds_them_once(self):
        # Another test module may have reloaded master in the meantime, so extend once here
        # and prove the second pass adds nothing.
        schedule.extend_cache()
        courses_before = len(cache.music_course_master)
        bands_before = len(cache.course_ranking_reward_master)
        schedule.extend_cache()
        self.assertEqual(len(cache.music_course_master), courses_before)
        self.assertEqual(len(cache.course_ranking_reward_master), bands_before)
        # The generated editions are in the cache the routes read.
        ids = {row.id_ for row in cache.music_course_master}
        self.assertTrue(all(row.id in ids for row in schedule.generated()))
        self.assertEqual(len(schedule.live()), 2)


class WeeklyRanking(unittest.TestCase):
    """The official rank bands, and the local field they are taken against."""

    def setUp(self):
        self.generated = schedule.generated()[0]

    def test_only_ranked_courses_get_a_row(self):
        self.assertEqual(len(courses.bands(356)), 7)          # an archived weekly edition
        self.assertEqual(len(courses.bands(200)), 4)          # 第1回 shipped four bands only
        full = next(row for row in schedule.generated() if len(courses.bands(row.id)) == 7)
        self.assertEqual(len(courses.bands(full.id)), 7)
        self.assertEqual(courses.bands(3009), [])             # a 段位認定 course: grade rewards

    def test_band_ranges_are_read_low_to_high(self):
        for rank, (low, high) in ((1, (1, 1)), (2, (2, 2)), (3, (3, 3)), (4, (4, 10)),
                                  (10, (4, 10)), (11, (11, 50)), (50, (11, 50)),
                                  (51, (51, 100)), (100, (51, 100)), (5000, (101, 9999999))):
            band = courses.band_for(356, rank)
            self.assertIsNotNone(band, f"rank {rank} has no band")
            self.assertEqual(sorted((band.target_highest_rank, band.target_lowest_rank)),
                             [low, high], f"rank {rank} in the wrong band")

    def test_the_generated_field_stays_inside_the_scale_the_archive_implies(self):
        rates = courses.field_rates(self.generated.id)
        self.assertEqual(len(rates), courses.RIVAL_COUNT)
        self.assertEqual(rates, sorted(rates, reverse=True))
        self.assertLessEqual(max(rates), courses.FIELD_TOP)
        self.assertLess(courses.FIELD_TOP, courses.CEILING)
        self.assertGreater(min(rates), courses.FIELD_BOTTOM - 1)
        self.assertEqual(rates, courses.field_rates(self.generated.id))
        placed = 1 + sum(1 for rate in rates if rate > 302.76)   # this account's archived best
        self.assertLessEqual(placed, 3)
        for rate, window in ((300.0, (4, 12)), (295.0, (40, 55)), (290.0, (78, 92))):
            rank = 1 + sum(1 for value in rates if value > rate)
            self.assertTrue(window[0] <= rank <= window[1], f"{rate} placed {rank}")

    def test_the_hit_columns_are_the_ranking_rows_own_columns(self):
        blocks = [type("Block", (), {"timing_type": timing})() for timing in (
            TimingTypes.PERFECT_STAR, TimingTypes.PERFECT_STAR, TimingTypes.PERFECT,
            TimingTypes.GREAT, TimingTypes.GOOD, TimingTypes.BAD, TimingTypes.MISS,
            TimingTypes.None_)]
        counts = courses.note_counts(blocks)
        self.assertEqual(counts, {"perfectStar": 2, "perfect": 1, "great": 1, "good": 1,
                                  "bad": 1, "miss": 1})
        self.assertEqual(courses.note_counts([]), dict.fromkeys(courses.COLUMNS, 0))

    def test_the_invented_parts_stay_in_their_own_tables(self):
        self.assertEqual(courses.RIVAL_LABEL, "ローカル対抗")
        for table in (courses.RIVAL_TABLE, courses.DRAW_TABLE):
            self.assertTrue(table.startswith("preservation_"), table)
        self.assertEqual(courses.RANK_TABLE, "music_course_ranking")


class RandomCourseDraw(unittest.TestCase):
    """The archived ランダムコース rows, whose songs the server has to pick."""

    def random_courses(self):
        return [row for row in cache.music_course_master
                if any(detail.random_difficulty for detail in row.details or [])]

    def test_the_archived_random_courses_are_the_expected_ones(self):
        self.assertEqual(sorted(row.id_ for row in self.random_courses()), [77, 78, 79, 80])

    def test_a_draw_sits_inside_every_details_own_band(self):
        live = {row.id_: row for row in cache.live_master}
        for course in self.random_courses():
            drawn = courses.draw(course)
            self.assertEqual([number for number, _ in drawn],
                             [detail.set_list_number for detail in
                              sorted(course.details, key=lambda d: d.set_list_number)])
            self.assertEqual(len({ident for _, ident in drawn}), len(drawn))
            for (number, ident), detail in zip(drawn, sorted(course.details,
                                                             key=lambda d: d.set_list_number)):
                self.assertEqual(int(live[ident].difficulty), int(detail.random_difficulty))
                self.assertEqual(int(live[ident].level), int(detail.random_level))
                self.assertNotIn(ident, schedule.UNPLAYABLE)

    def test_a_fixed_course_draws_its_own_songs(self):
        course = next(row for row in cache.music_course_master if row.id_ == 356)
        self.assertEqual([ident for _, ident in courses.draw(course)],
                         [detail.live_master_id for detail in course.details])


if __name__ == "__main__":
    unittest.main()
