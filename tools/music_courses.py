"""The weekly course's ranking, and the random chart draw the archived courses ask for.

Official part, straight from the recovered master and the protocol:

  * the course itself - three songs, one shared life pool, one 課題曲参加券 (item 130201)
    per attempt - which ``progression`` already runs;
  * the ranking entity the client already carries: ``music_course_ranking`` (union key 178)
    with the hit counts, ``totalAchievementRatePercentRecord`` and ``hasReceivedReward``,
    written once per course and updated per attempt;
  * the reward bands, ``CourseRankingRewardMaster``: one trophy per rank band (1, 2, 3,
    4-10, 11-50, 51-100, 101+) and only for the weekly and anniversary courses - which is
    why a generated edition carries a copy of a real edition's bands (course_schedule);
  * ``Lives/SelectMusicCourseRandomMusic``: the archived ランダムコース rows (ids 77-80,
    still open) have details with no song and instead a ``random_difficulty`` /
    ``random_level`` pair, and the client asks this route for the songs. It is a stub
    upstream, returning an empty result, so those four courses cannot be played at all.

Not official - the one invented part - is the field those ranks are taken against. An
official weekly ranking is between every player on the service and the recovery holds one
account, so the field is generated locally in preservation_course_rival and never written
into an official table. Its scale comes from the archive rather than from imagination: the
quantity ranked on is the total achievement rate of the three songs, whose ceiling is 101.0
per note (``helpers/live_result._RATE_WEIGHT``) = 303.0 for a three-song course, and this
account's 13 archived weekly records sit at 299.47..302.76. The generated field therefore
spans 288.0..301.0: clearing a course lands in the 51-100 band, a clean run climbs toward
11-50, and the 1st-place trophy asks for a better run than any this account archived. Real
accounts on this server are ranked in the same list, with their real records - and since the
field is 99 entrants, one account tops out at rank 100, which is why the master's final
101+ band stays unpaid until a second account posts a record here.
"""
import random
import time

from fastapi import APIRouter, Request
from helpers.cache import cache
from helpers.msgpack import fault, read_request, respond
from models import (MusicCourseRandomSelectResult, MusicCourseRandomSelectResultDetail,
                    SelectMusicCourseRandomMusicPayload)
from models.enums import TimingTypes

import course_schedule
from gameplay import transaction

router = APIRouter()

# The official ranking row; it is in the save already, with hasReceivedReward set.
RANK_TABLE = 'music_course_ranking'
# The generated field has its own table: it must never reach an official entity table.
RIVAL_TABLE = 'preservation_course_rival'
# The songs a random course drew for this account, so the live start can be checked against
# what the client was told.
DRAW_TABLE = 'preservation_course_draw'
# The label is deliberate: a generated entrant must never read like a recovered player.
RIVAL_LABEL = 'ローカル対抗'
RIVAL_COUNT = 99

# See the module docstring: 101.0 per note over three songs, and the archived records.
CEILING = 303.0
FIELD_TOP = 301.0
FIELD_BOTTOM = 288.0

# TimingTypes, worst to best, as the ranking row's columns.
COLUMN_FOR_TIMING = {
    int(TimingTypes.PERFECT_STAR): 'perfectStar',
    int(TimingTypes.PERFECT): 'perfect',
    int(TimingTypes.GREAT): 'great',
    int(TimingTypes.GOOD): 'good',
    int(TimingTypes.BAD): 'bad',
    int(TimingTypes.MISS): 'miss',
}
COLUMNS = tuple(COLUMN_FOR_TIMING.values())


def _now_micros():
    return int(time.time_ns() // 1_000_000)


# --- the bands --------------------------------------------------------------------------


def bands(course_id):
    """This course's rank bands, lowest rank first - empty unless it is a ranked course.

    The weekly courses have no MusicCourseRewardGroupMaster rows, so these are their whole
    reward table; the dan-class courses have none, which is how the master separates them.
    """
    return sorted(
        (band for band in cache.course_ranking_reward_master
         if int(band.music_course_master_id) == int(course_id)),
        key=lambda band: (int(band.target_highest_rank), int(band.target_lowest_rank)),
    )


def band_for(course_id, rank):
    for band in bands(course_id):
        low, high = sorted((int(band.target_highest_rank), int(band.target_lowest_rank)))
        if low <= int(rank) <= high:
            return band
    return None


def note_counts(blocks):
    """One run's hit counts, in the columns the ranking row stores."""
    counts = dict.fromkeys(COLUMNS, 0)
    for block in blocks or []:
        column = COLUMN_FOR_TIMING.get(int(block.timing_type))
        if column:
            counts[column] += 1
    return counts


# --- the local field --------------------------------------------------------------------


def field_rates(course_id):
    """The generated field's rates, best first: 288.0..301.0, laid out by seeded draw.

    Seeded by course id, so the field is the same for every restart and for every account
    on this server - which is what makes a rank here mean the same thing twice.
    """
    rng = random.Random(f'course:field:{course_id}')
    out = []
    for ordinal in range(1, RIVAL_COUNT + 1):
        span = (ordinal - 1) / (RIVAL_COUNT - 1)
        rate = FIELD_TOP - (FIELD_TOP - FIELD_BOTTOM) * span + rng.uniform(-0.35, 0.35)
        out.append(round(max(0.0, min(FIELD_TOP, rate)), 4))
    return sorted(out, reverse=True)


async def rivals(s, course_id):
    """This course's generated entrants, best rate first. Made once, then kept."""
    rows = await s.conn.conn.fetch(
        f'SELECT rate FROM "{RIVAL_TABLE}" WHERE "userId"=$1 AND "masterId"=$2 ORDER BY rate DESC',
        s.uid, int(course_id),
    )
    if rows:
        return [float(row['rate']) for row in rows]
    out = field_rates(course_id)
    for ordinal, rate in enumerate(out, start=1):
        await s.conn.conn.execute(
            f'INSERT INTO "{RIVAL_TABLE}" '
            '("userId", "masterId", ordinal, "userHash", name, rate) VALUES ($1,$2,$3,$4,$5,$6)',
            s.uid, int(course_id), ordinal, f'cr{course_id}{ordinal:02d}',
            f'{RIVAL_LABEL} {ordinal:02d}', rate,
        )
    return out


async def rank_of(s, course_id, rate):
    """Where a rate places: against the generated field and against real accounts here."""
    field = await rivals(s, course_id)
    ahead = sum(1 for value in field if value > rate)
    others = await s.conn.conn.fetch(
        f'SELECT "totalAchievementRatePercentRecord" AS rate FROM "{RANK_TABLE}" '
        'WHERE "musicCourseMasterId"=$1 AND "userId"<>$2',
        int(course_id), s.uid,
    )
    ahead += sum(1 for row in others if float(row['rate'] or 0) > rate)
    return ahead + 1, len(field) + 1 + len(others)


async def post_attempt(s, course, run, grade):
    """Record a finished attempt in the ranking row, and pay its rank band once.

    Only the courses that have bands get a row, which is what the archive shows too: its
    ranking rows are weekly courses, and the 段位認定 courses have none.
    """
    if not bands(course.id_):
        return None
    total = round(sum(run['rates']), 4)
    row = await s.one('MusicCourseRanking', musicCourseMasterId=course.id_)
    attempts = int(row['currentChallengeCount']) if row else 0
    record = float(row['totalAchievementRatePercentRecord'] or 0) if row else 0.0
    counts = dict.fromkeys(COLUMNS, 0)
    for notes in run.get('notes') or []:
        for column in COLUMNS:
            counts[column] += int(notes.get(column) or 0)
    values = {
        'currentChallengeCount': attempts + 1,
        **counts,
        'totalAchievementRatePercentRecord': str(max(record, total)),
    }
    if total >= record:
        values.update({'bestRecordChallengeCount': attempts + 1,
                       'bestRecordDate': _now_micros()})
    if row:
        await s.update('MusicCourseRanking', row, **values)
    else:
        row = await s.insert('MusicCourseRanking', musicCourseMasterId=course.id_,
                             hasReceivedReward=False, **values)
    rank, entrants = await rank_of(s, course.id_, float(values['totalAchievementRatePercentRecord']))
    reward = []
    if grade > 1 and not row['hasReceivedReward']:
        band = band_for(course.id_, rank)
        if band:
            reward = await s.grant(
                [(int(thing.thing_type), int(thing.thing_id), int(thing.thing_quantity))
                 for thing in band.rewards or []]
            )
        await s.update('MusicCourseRanking', row, hasReceivedReward=True)
    return {'rank': rank, 'entrants': entrants, 'things': reward}


# --- the random chart draw --------------------------------------------------------------


def pool(difficulty, level):
    """Charts a random detail may draw: this difficulty and level, and playable."""
    return [row.id_ for row in cache.live_master
            if int(row.difficulty) == int(difficulty) and int(row.level) == int(level)
            and row.id_ not in course_schedule.UNPLAYABLE]


def draw(course):
    """The songs for one attempt of a random course: one chart per detail's band.

    A fresh draw every time the client asks, which is what its random button is for; the
    draw is kept in DRAW_TABLE so the course start can be checked against it.
    """
    rng = random.Random()
    out = []
    for detail in sorted(course.details or [], key=lambda d: d.set_list_number):
        if detail.live_master_id:
            out.append((int(detail.set_list_number), int(detail.live_master_id)))
            continue
        candidates = pool(detail.random_difficulty or 0, detail.random_level or 0)
        if not candidates:
            return None
        out.append((int(detail.set_list_number), rng.choice(candidates)))
    return out


async def store_draw(s, course_id, gauge, details):
    await s.conn.conn.execute(
        f'INSERT INTO "{DRAW_TABLE}" ("userId", "masterId", gauge, details, "drawnAt") '
        'VALUES ($1,$2,$3,$4,$5) ON CONFLICT ("userId", "masterId", gauge) '
        'DO UPDATE SET details=$4, "drawnAt"=$5',
        s.uid, int(course_id), int(gauge), details, _now_micros(),
    )


async def stored_draw(s, course_id, gauge):
    row = await s.conn.conn.fetchrow(
        f'SELECT details FROM "{DRAW_TABLE}" WHERE "userId"=$1 AND "masterId"=$2 AND gauge=$3',
        s.uid, int(course_id), int(gauge),
    )
    return row['details'] if row else None


async def clear_draw(s, course_id):
    await s.conn.conn.execute(
        f'DELETE FROM "{DRAW_TABLE}" WHERE "userId"=$1 AND "masterId"=$2', s.uid, int(course_id)
    )


async def chart_matches(s, course, detail, live_master_id, gauge):
    """Is this chart allowed for this detail of this course?

    A fixed detail has to play its own song. A random one plays what the draw handed out;
    if the client never called the draw route, anything inside that detail's difficulty and
    level band is accepted, because that is the band the master row itself declares.
    """
    if detail.live_master_id:
        return int(detail.live_master_id) == int(live_master_id)
    drawn = await stored_draw(s, course.id_, gauge)
    if drawn:
        return any(int(entry['set_list_number']) == int(detail.set_list_number)
                   and int(entry['live_master_id']) == int(live_master_id) for entry in drawn)
    chart = next((row for row in cache.live_master if row.id_ == int(live_master_id)), None)
    return bool(chart and int(chart.difficulty) == int(detail.random_difficulty or 0)
                and int(chart.level) == int(detail.random_level or 0))


@router.post('/api/Lives/SelectMusicCourseRandomMusic', name='Lives_SelectMusicCourseRandomMusic')
async def select_random_music(request: Request):
    payload = await read_request(request, SelectMusicCourseRandomMusicPayload)
    course_id = int(getattr(payload, 'music_course_master_id', 0) or 0)
    course = next((row for row in cache.music_course_master if row.id_ == course_id), None)
    if course is None:
        return respond(MusicCourseRandomSelectResult(), faults=[fault('InvalidCourse')])
    gauge = int(getattr(payload, 'music_course_gauge_type', 0) or 0)
    drawn = draw(course)
    if drawn is None:
        return respond(MusicCourseRandomSelectResult(), faults=[fault('InvalidCourse')])
    async with transaction(request) as s:
        await store_draw(s, course.id_, gauge, [
            {'set_list_number': number, 'live_master_id': live} for number, live in drawn
        ])
    return respond(MusicCourseRandomSelectResult(details=[
        MusicCourseRandomSelectResultDetail(set_list_number=number, live_master_id=live)
        for number, live in drawn
    ]))


# --- storage and install ----------------------------------------------------------------


async def ensure_tables(conn):
    """The two local tables - never an official entity table."""
    await conn.conn.execute(
        f'CREATE TABLE IF NOT EXISTS "{RIVAL_TABLE}" ('
        '"userId" bigint NOT NULL REFERENCES accounts("userId") ON DELETE CASCADE, '
        '"masterId" integer NOT NULL, ordinal integer NOT NULL, "userHash" text NOT NULL, '
        'name text NOT NULL, rate numeric(8,4) NOT NULL, '
        'PRIMARY KEY ("userId", "masterId", ordinal))'
    )
    await conn.conn.execute(
        f'CREATE TABLE IF NOT EXISTS "{DRAW_TABLE}" ('
        '"userId" bigint NOT NULL REFERENCES accounts("userId") ON DELETE CASCADE, '
        '"masterId" integer NOT NULL, gauge integer NOT NULL, details jsonb NOT NULL, '
        '"drawnAt" bigint NOT NULL, PRIMARY KEY ("userId", "masterId", gauge))'
    )


def install(app):
    """Extend the master cache with the generated editions, add the local tables, route."""
    from contextlib import asynccontextmanager

    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with previous(app):
            course_schedule.extend_cache()
            async with app.acquire_db() as conn:
                await ensure_tables(conn)
            yield
    app.router.lifespan_context = lifespan

    count = len(app.router.routes)
    app.include_router(router)
    app.router.routes[:] = app.router.routes[count:] + app.router.routes[:count]


from gameplay import transaction  # noqa: E402  (gameplay imports this module, so import late)
