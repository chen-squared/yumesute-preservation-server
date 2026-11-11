"""Generated MusicCourseMaster rows, so the weekly course keeps running past the archive.

The archive's 「第N回 ウィークリーコース」 chain stops with the service: the last edition
is 第79回 (ids 356/357) and its window closed 2026-09-21, so the course screen is left with
only the permanent 段位認定 courses. Everything a generated edition is made of is official -
the name, both departments, the three songs drawn from the charts the archived weekly
courses really used, the 300 point shared life, the one 課題曲参加券 (item 130201) per
attempt, and the trophy bands in CourseRankingRewardMaster, copied from a real edition.
Only two things come from us: the id and the calendar.

The calendar is measured, not invented. All 158 archived weekly rows are 7 days apart, and
the 156 regular ones all run from Monday 17:00 to Monday 00:00 in the master's wall clock
(151 hours), two rows per edition - one 【STELLA部門】, one 【OLIVIER部門】, ids in sequence;
``order`` is only a display sort key and wanders from 1 to 22 across editions. The generated
chain therefore continues that phase from the last official start, and an edition's ids are
its position in the chain, stable across restarts, which matters because account rows
reference the course id.

The songs are dealt at random, which is the whole point of the weekly course here: three
different charts from the department's own archived pool (measured rule - STELLA always
plays difficulty 4 at levels 23..29, OLIVIER always difficulty 5 at levels 101..108),
seeded by course id so the draw is the same for every account on this server and for every
restart of it. That stability is what the local ranking in ``music_courses`` ranks against.
"""
import json
import random
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from helpers.cache import cache
from models.master_data import CourseRankingRewardMaster, MusicCourseMaster

# iso/packed/seconds are the same master-injection helpers the ladder uses; one definition.
from league_schedule import _seconds, iso, packed

MASTERDATA = Path(__file__).resolve().parents[1] / 'vendor/server-of-dreams/_data/masterdata'

DAY = 86400
STEP = 7 * DAY
# Measured: every regular archived weekly row runs Monday 17:00 -> Monday 00:00.
WINDOW = 151 * 3600
# How far ahead the chain is published.
AHEAD_DAYS = 42

# MusicCourseType.WeeklyCourseRanking: the type the course screen's weekly tab lists, and
# the only type that has CourseRankingRewardMaster rows.
COURSE_TYPE = 5

# Charts the recovery has no notation data for, so they cannot be played; the archived
# weekly pool references one of them.
UNPLAYABLE = {3701, 4205, 13505, 14301, 16601, 17501, 17903}


class Edition(NamedTuple):
    id: int
    name: str
    order: int
    initial_life: int
    item: int
    amount: int
    start: int
    end: int
    lives: tuple
    # The archived course this edition's name and trophy bands were taken from.
    source: int
    official: bool


@lru_cache(maxsize=None)
def _live_rows():
    return {int(row['id']): row
            for row in json.loads((MASTERDATA / 'LiveMaster.json').read_text(encoding='utf-8'))}


@lru_cache(maxsize=None)
def archived():
    """The weekly chain exactly as the archive shipped it, in time order.

    Read from the master JSON rather than the in-process cache, so the generated chain is
    the same whether it is asked for while packing the master download or while answering
    a request.
    """
    rows = json.loads((MASTERDATA / 'MusicCourseMaster.json').read_text(encoding='utf-8'))
    return sorted((row for row in rows if _weekly(row['name'])),
                  key=lambda row: (row['start_date'], row['order']))


def _weekly(name):
    return bool(re.match(r'第\d+回 ウィークリーコース', name or ''))


@lru_cache(maxsize=None)
def departments():
    """department label -> difficulty, level span and playable chart pool, measured.

    Over all 158 archived weekly rows 【STELLA部門】 always plays difficulty 4 (levels
    23..29) and 【OLIVIER部門】 always difficulty 5 (levels 101..108). The pool is the charts
    the archived weekly courses actually used, so every generated course is made of songs
    the weekly ladder has already put in front of players. The department is read out of the
    name: ``order`` is only a display sort key and wanders from 1 to 22 across editions.
    """
    seen = {}
    for row in archived():
        label = re.search(r'【(.+?)】', row['name']).group(1)
        seen.setdefault(label, set()).update(int(detail['live_master_id'])
                                             for detail in row['details'])
    out = {}
    for label, charts in seen.items():
        pool = sorted(chart for chart in charts if chart not in UNPLAYABLE)
        difficulties = {int(_live_rows()[chart]['difficulty']) for chart in pool}
        levels = sorted(int(_live_rows()[chart]['level']) for chart in pool)
        out[label] = {'pool': pool, 'difficulties': sorted(difficulties),
                      'levels': (min(levels), max(levels))}
    return out


def department(name):
    return re.search(r'【(.+?)】', name).group(1)


@lru_cache(maxsize=None)
def editions():
    """The archived chain as editions: (number, rows ordered by department order)."""
    groups = {}
    for row in archived():
        groups.setdefault(int(re.match(r'第(\d+)回', row['name']).group(1)), []).append(row)
    return [(number, sorted(rows, key=lambda row: int(row['order'])))
            for number, rows in sorted(groups.items())]


def anchor():
    """The last official edition: its ids, name and start phase seed the generated chain."""
    return editions()[-1]


def songs(course_id, label):
    """The three charts of a generated row: random, one per song, never the same song twice."""
    pool = departments()[label]['pool']
    by_music = {}
    for chart in pool:
        by_music.setdefault(int(_live_rows()[chart]['music_master_id']), []).append(chart)
    musics = sorted(by_music)
    rng = random.Random(f'course:songs:{course_id}')
    rng.shuffle(musics)
    return tuple(rng.choice(by_music[music]) for music in musics[:3])


def generated(now=None):
    """The chain that carries the weekly course past the archive's last edition."""
    last_number, templates = anchor()
    last_start = max(_seconds(row['start_date']) for row in templates)
    horizon = (now if now is not None else _now()) + AHEAD_DAYS * DAY
    out, index = [], 1
    while last_start + index * STEP < horizon:
        start = last_start + index * STEP
        number = last_number + index
        source_number, sources = editions()[(index - 1) % len(editions())]
        for position, row in enumerate(templates):
            label = department(row['name'])
            source = next(r for r in sources if department(r['name']) == label)
            course_id = int(row['id']) + index * len(templates)
            out.append(Edition(
                id=course_id,
                name=row['name'].replace(f'第{last_number}回', f'第{number}回'),
                order=int(row['order']),
                initial_life=int(row['initial_life']),
                item=row['required_item_master_id'],
                amount=int(row['required_amount']),
                start=start,
                end=start + WINDOW,
                lives=songs(course_id, label),
                source=int(source['id']),
                official=False,
            ))
        index += 1
    return out


def _now():
    return int(time.time_ns() // 1_000_000_000)


def live(now=None):
    now = now if now is not None else _now()
    return [row for row in generated(now) if row.start <= now < row.end]


def find(course_id, now=None):
    return next((row for row in generated(now) if row.id == int(course_id)), None)


# --- injection: the client reads these through the master data download -----------------

def json_rows():
    """Generated editions as master-data JSON dicts (for the in-process cache)."""
    return [
        {
            'id': row.id,
            'name': row.name,
            'music_course_type': COURSE_TYPE,
            'order': row.order,
            'initial_life': row.initial_life,
            'required_item_master_id': row.item,
            'required_amount': row.amount,
            'start_date': iso(row.start),
            'end_date': iso(row.end),
            'unlock_condition': 0,
            'unlock_condition_value': None,
            'tournament_qualifying_master_id': None,
            'details': [
                {
                    'id': row.id * 100 + position,
                    'set_list_number': position,
                    'live_master_id': live_master_id,
                    'vocal_version': 1,
                    'another_notation_master_id': 0,
                    'random_difficulty': None,
                    'random_level': None,
                }
                for position, live_master_id in enumerate(row.lives, start=1)
            ],
        }
        for row in generated()
    ]


def ranking_reward_rows():
    """Rank bands for the generated ids, copied from each edition's source course.

    The weekly courses have no MusicCourseRewardGroupMaster rows at all - their rewards are
    the seven CourseRankingRewardMaster trophy bands (1st, 2nd, 3rd, 4-10, 11-50, 51-100,
    101+), one per rank, so a generated edition has to carry its own copy or there would be
    nothing to pay. The trophies themselves are the archived edition's, never invented ones.
    """
    official = json.loads((MASTERDATA / 'CourseRankingRewardMaster.json').read_text(encoding='utf-8'))
    by_course = {}
    for row in official:
        by_course.setdefault(int(row['music_course_master_id']), []).append(row)
    rows = []
    for edition in generated():
        bands = sorted(by_course.get(edition.source, []),
                       key=lambda row: (int(row['target_highest_rank']),
                                        int(row['target_lowest_rank'])))
        for position, band in enumerate(bands, start=1):
            band_id = edition.id * 10 + position
            rows.append({
                'id': band_id,
                'music_course_master_id': edition.id,
                'target_highest_rank': int(band['target_highest_rank']),
                'target_lowest_rank': int(band['target_lowest_rank']),
                # The band's contents are the source edition's own trophies; only their row
                # numbers are re-issued, so no two band rows carry the same nested id.
                'rewards': [
                    {'id': band_id * 10 + place, 'thing_type': int(thing['thing_type']),
                     'thing_id': thing['thing_id'], 'thing_quantity': thing['thing_quantity']}
                    for place, thing in enumerate(band.get('rewards') or [], start=1)
                ],
            })
    return rows


def extend_tables(tables, changes):
    """Append the generated editions and their bands to an unpacked master db."""
    for row in json_rows():
        tables['MusicCourseMaster'].append(packed('MusicCourseMaster', row))
        changes.append(('MusicCourseMaster', row['id'], 'generated-weekly-course'))
    taken = {row[0] for row in tables['CourseRankingRewardMaster']}
    for row in ranking_reward_rows():
        if row['id'] in taken:
            continue
        tables['CourseRankingRewardMaster'].append(packed('CourseRankingRewardMaster', row))
        changes.append(('CourseRankingRewardMaster', row['id'], 'generated-weekly-bands'))


def extend_cache():
    """Make the generated editions visible to master() in this process."""
    courses = cache.music_course_master
    known = {row.id_ for row in courses}
    for row in json_rows():
        if row['id'] not in known:
            courses.append(MusicCourseMaster.model_validate(row))
    bands = cache.course_ranking_reward_master
    known = {row.id_ for row in bands}
    for row in ranking_reward_rows():
        if row['id'] not in known:
            bands.append(CourseRankingRewardMaster.model_validate(row))
