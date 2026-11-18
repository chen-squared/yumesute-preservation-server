"""The character-birthday calendar: official dates, rebuilt for the year being lived through.

``preservation_dates`` moves the birthday windows - the ``SplashMaster`` movie, the
``TitleBackgroundMaster`` title screen and the ``TitleCallVoiceMaster`` greeting - from the single
day each held in 2024..2026 onto the same day of the serving year. This test pins what a birthday is
made of in the recovered master (21 splash rows, one per character; 50 title rows; 26 greeting rows
tied to a character by their voice id and their date), that the date, the hour and the window's
length survive the move while only the year changes, that the twelve birthday movies the 2.31.3
client cannot reach stay closed while that character's title screen still opens, that the greeting
which already runs to the 2100 sentinel is left alone, that a window the archive already dates onto
the year is left alone rather than re-dated, and that no other row of these three tables moves. The
optional ``YUMESUTE_BIRTHDAY`` viewing aid gets its own two tests.

Reads no database. Whether a splash actually plays on a device is checked in game.
"""
import datetime
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "vendor/server-of-dreams"))

from helpers.mastermemory import unpack
from models.keys import KEYS

import birthday_calendar as born
import preservation_dates as dates

YEAR = 2027
# Measured twice and agreeing: the birthday movies the current addressables catalogue
# (asset version 1.96.0) lists, which are exactly the ones whose .usm and addressable bundle are
# both in the archive. The other twelve .usm files are here but unreachable for this client.
ARCHIVED_MOVIES = {102, 104, 106, 201, 203, 302, 303, 402, 405}
# The characters behind them, from the same splash rows.
CLOSED_MOVIES = {101, 103, 105, 202, 204, 205, 301, 304, 305, 401, 403, 404}
# Greeting rows tied to a birthday, and the one of them that is open every day as archived
# (TitleCallVoiceMaster 102 runs to the 2100 sentinel) and so is never moved.
GREETINGS = 26
ALWAYS_OPEN_GREETING = 102
# What one year's calendar opens: the newest edition of each date, and only the nine movies this
# client can reach. Measured from the archive, and pinned here so a silent change in either shows up.
OPENED = {"SplashMaster": 9, "TitleBackgroundMaster": 25, "TitleCallVoiceMaster": 21}


def original():
    """The archived master, before any preservation change."""
    return unpack((ROOT / "private/upstream/master-original.db").read_bytes())


def served(year=YEAR):
    """The master the server would hand the client for one birthday calendar year."""
    return unpack(dates.preserved(year)[0])


def columns(table):
    return {field: key for key, field, *_ in KEYS[table]}


def by_id(table, tables):
    return {row[0]: row for row in tables[table]}


def clock(stamp):
    """A master DateTime as the wall clock it is stored to be."""
    return datetime.datetime.fromtimestamp(stamp.seconds, datetime.timezone.utc)


class Birthday(unittest.TestCase):
    def setUp(self):
        self.archive, self.now = original(), served()
        self.found = born.birthday_rows(self.archive)[0]
        self.fields = {name: columns(name) for name in born.TABLES}

    def splash_rows(self, tables):
        fields = columns(born.SPLASH)
        return [row for row in tables[born.SPLASH]
                if str(row[fields["splash_value"]]).startswith(born.MOVIE_PREFIX)]

    def test_a_birthday_is_a_movie_a_title_screen_and_a_greeting(self):
        self.assertEqual(len(self.found[born.SPLASH]), 21)
        self.assertEqual(len({born.character(row) for row in self.found[born.SPLASH]}), 21,
                         "one birthday splash per character, none twice")
        self.assertEqual(len(self.found[born.TITLE]), 50,
                         "the 2026 re-runs are their own rows naming the older art")
        self.assertEqual(len(self.found[born.VOICE]), GREETINGS)
        self.assertEqual(len({row[columns(born.VOICE)["voice_id"]] for row in self.found[born.VOICE]}),
                         21, "one line per character; four of them were re-run the next year")

    def test_the_greeting_rows_are_named_by_two_facts(self):
        on = born.birthday_rows(self.archive)[1]
        self.assertEqual(on[(9, 13)], {105, 202}, "two characters share 13 September")
        voice = columns(born.VOICE)
        for row in self.found[born.VOICE]:
            day = born.day_of(row[voice["start_date"]])
            self.assertIn(int(row[voice["voice_id"]]) // 1000, on[day],
                          "the voice id names a character whose birthday is that day")
        birthday_ids = {row[0] for row in self.found[born.VOICE]}
        left = [row for row in self.now[born.VOICE] if row[0] not in birthday_ids]
        self.assertEqual(len(left), len(self.archive[born.VOICE]) - GREETINGS)
        for row in left:
            self.assertEqual(row, by_id(born.VOICE, self.archive)[row[0]],
                             "the greetings that are not birthdays keep their archived windows")
        shared = [row for row in left if born.day_of(row[voice["start_date"]]) == (7, 26)
                  and int(row[voice["voice_id"]]) // 1000 != 102]
        self.assertTrue(shared, "the set naming every character on 2024-07-26 stays where it is")
        always = by_id(born.VOICE, self.now)[ALWAYS_OPEN_GREETING]
        self.assertEqual(always, by_id(born.VOICE, self.archive)[ALWAYS_OPEN_GREETING],
                         "the greeting that runs to the 2100 sentinel is already open every day")

    def test_every_birthday_window_keeps_its_date_and_length(self):
        moved = {}
        for name, rows in born.editions(self.found).items():
            for was in rows:
                if not born.shows(name, was) or born.permanent(was, self.fields[name]):
                    continue
                moved[name] = moved.get(name, 0) + 1
                is_now = by_id(name, self.now)[was[0]]
                for field in ("start_date", "end_date"):
                    key = self.fields[name][field]
                    before, after = clock(was[key]), clock(is_now[key])
                    self.assertEqual((after.month, after.day, after.hour, after.minute),
                                     (before.month, before.day, before.hour, before.minute),
                                     f"{name} {was[0]} {field} keeps its date and hour")
                    self.assertEqual(after.year, YEAR, field)
                    self.assertEqual(is_now[key].nanoseconds, was[key].nanoseconds, field)
                def length(row):
                    return (row[self.fields[name]["end_date"]].seconds
                            - row[self.fields[name]["start_date"]].seconds)
                self.assertEqual(length(is_now), length(was),
                                 f"{name} {was[0]} keeps the window it officially had")
        self.assertEqual(moved, OPENED)

    def test_only_the_newest_edition_of_a_date_is_opened(self):
        fields = columns(born.TITLE)
        start = fields["start_date"]
        jan20 = [row for row in self.found[born.TITLE] if born.day_of(row[start]) == (1, 20)]
        self.assertEqual(len(jan20), 2, "the archive holds that title screen for two years")
        older, newer = sorted(jan20, key=lambda row: row[start].seconds)
        self.assertEqual(by_id(born.TITLE, self.now)[older[0]], older,
                         "the year that has been lived through stays where it is")
        self.assertEqual(clock(by_id(born.TITLE, self.now)[newer[0]][start]).year, YEAR,
                         "the newest edition is the one that moves to this year")

    def test_a_window_that_already_holds_the_year_is_not_re_dated(self):
        """The master's newest edition is already dated 2026, so that year needs almost no work."""
        tables, changes = original(), []
        born.rotate(tables, changes, 2026)
        reported = {}
        for name, ident, what in changes:
            self.assertEqual(what, "birthday-window-2026")
            reported[name] = reported.get(name, 0) + 1
        self.assertEqual(reported, {"TitleBackgroundMaster": 5, "TitleCallVoiceMaster": 5})
        was = {name: by_id(name, self.archive) for name in born.TABLES}
        mine = {name: {row[0] for row in self.found[name]} for name in born.TABLES}
        for name in born.TABLES:
            for row in tables[name]:
                if row[0] in mine[name] and was[name][row[0]] != row:
                    self.assertEqual(clock(row[self.fields[name]["start_date"]]).year, 2026,
                                     f"{name} {row[0]} was touched but not dated onto the year")
        on_year = {name: sum(1 for row in tables[name]
                             if row[0] in mine[name]
                             and not born.permanent(row, self.fields[name])
                             and born.shows(name, row)
                             and clock(row[self.fields[name]["start_date"]]).year == 2026)
                   for name in born.TABLES}
        self.assertEqual(on_year, {"SplashMaster": 9, "TitleBackgroundMaster": 25,
                                  "TitleCallVoiceMaster": 21},
                         "the same 55 windows open this year, 45 of them never touched")
        tables, changes = original(), []
        born.rotate(tables, changes, 2027)
        counted = {}
        for name, ident, what in changes:
            counted[name] = counted.get(name, 0) + 1
        self.assertEqual(counted, OPENED, "the following year has to re-date every one of them")

    def test_the_archive_had_one_day_windows_apart_from_two_short_last_days(self):
        lengths = set()
        for name, rows in self.found.items():
            for row in rows:
                was = by_id(name, self.archive)[row[0]]
                if born.permanent(was, self.fields[name]):
                    continue
                lengths.add(was[self.fields[name]["end_date"]].seconds
                            - was[self.fields[name]["start_date"]].seconds)
        self.assertIn(86400, lengths, "a birthday is one day")
        self.assertEqual(lengths - {86400}, {54000, 82800},
                         "one title screen was cut to 15 hours and another started an hour late")

    def test_the_movies_this_client_cannot_reach_stay_closed(self):
        fields = columns(born.SPLASH)
        stayed = {born.character(row) for row in self.splash_rows(self.archive)
                  if by_id(born.SPLASH, self.archive)[row[0]] == by_id(born.SPLASH, self.now)[row[0]]}
        self.assertEqual(stayed, CLOSED_MOVIES)
        self.assertEqual({ident: born.movie(str(next(
            r[fields["splash_value"]] for r in self.splash_rows(self.archive)
            if born.character(r) == ident)).lower()) for ident in stayed | ARCHIVED_MOVIES},
                         {**{i: True for i in ARCHIVED_MOVIES}, **{i: False for i in CLOSED_MOVIES}},
                         "the gap is the catalogue's, and movie() is a file check")
        for row in self.splash_rows(self.archive):
            ident = born.character(row)
            if ident not in CLOSED_MOVIES:
                continue
            day = clock(row[fields["start_date"]])
            title = [r for r in self.found[born.TITLE]
                     if (clock(r[self.fields[born.TITLE]["start_date"]]).month,
                         clock(r[self.fields[born.TITLE]["start_date"]]).day) == (day.month, day.day)]
            self.assertTrue(title, f"{ident} has no title screen at all")
            self.assertTrue(any(by_id(born.TITLE, self.now)[r[0]] != r for r in title),
                            f"{ident}'s title screen should still open on that date")

    def test_no_other_row_of_these_three_tables_moves(self):
        for name in born.TABLES:
            birthday_ids = {row[0] for row in self.found[name]}
            for row in self.now[name]:
                if row[0] not in birthday_ids:
                    self.assertEqual(row, by_id(name, self.archive)[row[0]],
                                     f"{name} {row[0]} is not birthday content and must not move")

    def test_the_changes_are_reported_as_birthday_windows(self):
        reported = [(name, ident, what) for name, ident, what in dates.preserved(YEAR)[2]
                    if name in born.TABLES]
        self.assertTrue(all(what == f"birthday-window-{YEAR}" for _, _, what in reported))
        counted = {}
        for name, ident, what in reported:
            counted[name] = counted.get(name, 0) + 1
        self.assertEqual(counted, OPENED)

    def test_the_year_is_the_cache_key(self):
        self.assertIs(dates.preserved(YEAR), dates.preserved(YEAR), "one year is built once")
        self.assertNotEqual(dates.preserved(YEAR)[1], dates.preserved(YEAR + 1)[1],
                            "a new birthday year is a new master version, once a year")
        fields = columns(born.SPLASH)
        row = next(r for r in self.splash_rows(served(YEAR + 1))
                   if str(r[fields["splash_value"]]) == "HappyBirthday_2026_102")
        self.assertEqual(clock(row[fields["start_date"]]).strftime("%Y-%m-%d %H:%M"), "2028-07-26 00:00")
        self.assertEqual(dates.serving_year(), datetime.datetime.now(datetime.timezone.utc).year)

    def test_the_media_behind_every_opened_window_is_on_disk(self):
        for row in self.found[born.TITLE]:
            self.assertTrue(born.pictures(row[self.fields[born.TITLE]["details"]]),
                            f"title art is missing for TitleBackgroundMaster {row[0]}")
        for row in self.found[born.SPLASH]:
            value = str(row[self.fields[born.SPLASH]["splash_value"]])
            self.assertEqual(born.movie(value), born.character(row) in ARCHIVED_MOVIES, value)

    def opened(self, watch):
        """The served master with the viewing aid on, and which tables it opened."""
        tables, changes = original(), []
        born.rotate(tables, changes, YEAR, watch=watch)
        return tables, changes

    def test_the_viewing_aid_opens_one_character_for_one_day(self):
        tables, changes = self.opened(102)
        fields = columns(born.SPLASH)
        self.assertEqual({name for name, _, what in changes if what == "birthday-today-102"},
                         set(born.TABLES), "movie, title screen and greeting together")
        row = next(r for r in self.splash_rows(tables)
                   if str(r[fields["splash_value"]]) == "HappyBirthday_2026_102")
        start, end = row[fields["start_date"]].seconds, row[fields["end_date"]].seconds
        self.assertLessEqual(start, time.time(), "the window is open now")
        self.assertLess(time.time(), end)
        self.assertEqual(end - start, 86400, "and it is still the official day")
        others = [r for r in self.splash_rows(tables)
                  if r is not row and born.movie(r[fields["splash_value"]])]
        self.assertEqual(len(others), len(ARCHIVED_MOVIES) - 1)
        self.assertTrue(all(clock(r[fields["start_date"]]).year == YEAR for r in others),
                        "the rest of the calendar rotates as usual around the one being watched")
        self.assertEqual([r for r in others
                          if clock(r[fields["start_date"]]).timestamp() <= time.time()
                          < clock(r[fields["end_date"]]).timestamp()],
                         [], "one birthday at a time")

    def test_the_viewing_aid_still_cannot_show_a_movie_that_is_not_there(self):
        tables, changes = self.opened(101)
        self.assertEqual({name for name, _, what in changes if what == "birthday-today-101"},
                         {born.TITLE, born.VOICE},
                         "the movie is unreachable, so only the screen and greeting are opened")
        row = next(r for r in self.splash_rows(tables) if born.character(r) == 101)
        self.assertEqual(row, by_id(born.SPLASH, original())[row[0]],
                         "the unreachable splash row is not opened at all - its movie cannot be played")


if __name__ == "__main__":
    unittest.main()
