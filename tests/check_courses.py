"""Destructive weekly-course checks ONLY in a disposable yumesute_course_checks DB.

This is the weekly music course end to end: the generated edition the client is shown, the
three songs it plays, the attempt that lands in the official ``music_course_ranking`` row,
the rank band its trophies come from, the local field the rank is taken against, and the
random draw the archived ランダムコース courses ask for and upstream never answered.
test_course_schedule.py pins the rules against master; this check shows those rules land in
the real schema, on a copy of the recovered save, through the same routes the iPad calls.

Make the scratch database from the live one first - the check mutates that copy and nothing
else:

    docker compose exec -T db psql -U yumesute -d postgres -c 'CREATE DATABASE yumesute_course_checks'
    docker compose exec -T db sh -c 'pg_dump -U yumesute -d yumesute | psql -qU yumesute -d yumesute_course_checks'

Then:  uv run --locked python tests/check_courses.py
Afterwards:  docker compose exec -T db psql -U yumesute -d postgres -c 'DROP DATABASE yumesute_course_checks'

Never point it at a save you intend to keep: it starts lives, finishes them, spends course
tickets and grants trophies.
"""
import asyncio
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = "yumesute_course_checks"
ACCOUNT = 5377823715
RANKING_UNION_KEY = 178          # models/unions.py: MusicCourseRanking
TICKET = 130201                  # 課題曲参加券
RANDOM_COURSE = 77               # ランダムコース～スタンダード～ 【STELLA部門】
NOTES = 60                       # score blocks per simulated song


def force_scratch_database():
    """Point the app's own config at the scratch database before anything connects.

    The name is replaced unconditionally, so this file can never run against a save.
    """
    from helpers.config import config, database

    config["database"]["database"] = SCRATCH
    database.database = SCRATCH


async def main():
    sys.path[:0] = [str(ROOT), str(ROOT / "tools"), str(ROOT / "vendor/server-of-dreams")]
    import httpx
    from helpers.auth import make_session_jwt
    from helpers.cache import cache, load_master_data
    from helpers.msgpack import pack
    from protocol import envelope, expand, unpack_stream

    force_scratch_database()
    import serve  # the production app, now bound to the scratch database
    import course_schedule
    import music_courses
    from models import (BaseScoreBlock, FinishLivePayload, SelectMusicCourseRandomMusicPayload,
                        StartLivePayload)
    from models.enums import TimingTypes

    app = serve.app
    # The app loads master data and extends the master cache inside its lifespan; this run
    # drives the app without one, so do both here.
    load_master_data()
    await app.yume_setup()
    course_schedule.extend_cache()   # after the load, so the generated editions survive it
    async with app.acquire_db() as conn:
        await music_courses.ensure_tables(conn)
        tickets = await conn.conn.fetchval('SELECT stock FROM item WHERE "userId"=$1 '
                                           'AND "itemMasterId"=$2', ACCOUNT, TICKET)
        running = await conn.conn.fetchval('SELECT count(*) FROM preservation_course_run '
                                           'WHERE "userId"=$1', ACCOUNT)
    assert tickets is not None, f"{SCRATCH} must be a copy of the live save"
    assert not running, "the clone already has a course attempt in progress"
    print(f"PASS: running against {SCRATCH} ({tickets} x 課題曲参加券)")

    charts = {row.id_: row for row in cache.live_master}
    songs_of = {music.id_: music.name for music in cache.music_master}

    def chart(ident):
        return charts[int(ident)]

    async def ranking_row():
        async with app.acquire_db() as conn:
            row = await conn.conn.fetchrow(
                'SELECT "musicCourseMasterId","currentChallengeCount","perfectStar","perfect",'
                '"great","good","bad","miss","totalAchievementRatePercentRecord",'
                '"bestRecordChallengeCount","bestRecordDate","hasReceivedReward" '
                'FROM music_course_ranking WHERE "userId"=$1 AND "musicCourseMasterId"=$2',
                ACCOUNT, course.id_)
        return dict(row) if row else None

    async def active_live():
        async with app.acquire_db() as conn:
            row = await conn.conn.fetchrow('SELECT * FROM active_live WHERE "userId"=$1', ACCOUNT)
        return dict(row) if row else None

    async def run_row():
        async with app.acquire_db() as conn:
            row = await conn.conn.fetchrow('SELECT data FROM preservation_course_run WHERE '
                                           '"userId"=$1', ACCOUNT)
        return row["data"] if row else None

    async def trophies():
        async with app.acquire_db() as conn:
            rows = await conn.conn.fetch('SELECT "trophyMasterId" FROM trophy WHERE "userId"=$1',
                                         ACCOUNT)
        return Counter(r["trophyMasterId"] for r in rows)

    async def field():
        async with app.acquire_db() as conn:
            rows = await conn.conn.fetch('SELECT ordinal,name,rate FROM preservation_course_rival '
                                         'WHERE "userId"=$1 AND "masterId"=$2 ORDER BY rate DESC',
                                         ACCOUNT, course.id_)
        return [dict(r) for r in rows]

    async def tickets_now():
        async with app.acquire_db() as conn:
            return await conn.conn.fetchval('SELECT stock FROM item WHERE "userId"=$1 AND '
                                            '"itemMasterId"=$2', ACCOUNT, TICKET)

    # --- the edition the client is shown this week --------------------------------------
    open_now = course_schedule.live()
    assert len(open_now) == 2, [row.name for row in open_now]
    course = next(row for row in cache.music_course_master if row.id_ == open_now[0].id)
    details = sorted(course.details, key=lambda d: d.set_list_number)
    songs = [(d.id_, int(d.live_master_id)) for d in details]
    bands = music_courses.bands(course.id_)
    assert len(songs) == 3 and len(bands) >= 4, (len(songs), len(bands))
    setlist = ", ".join(
        f"{songs_of[chart(live).music_master_id]} (難易度{chart(live).difficulty}-"
        f"{chart(live).level})" for _, live in songs)
    print(f"PASS: the course screen lists {course.name}: {setlist}; life {course.initial_life}, "
          f"{course.required_amount} x 課題曲参加券, {len(bands)} rank bands")

    def chained(blocks, fields):
        """Score blocks whose hash chain the server's verifier accepts."""
        running = 0
        for block in blocks:
            running += sum(int(getattr(block, name)) for name in fields)
            block.hash = running
        return blocks

    def perfect():
        """An all-PERFECT_STAR run: 101.0 per note, the ceiling of the achievement rate."""
        return FinishLivePayload(
            score=101 * NOTES, max_combo=NOTES, is_cleared=True, judges=[],
            base_score_blocks=chained(
                [BaseScoreBlock(score=101, life=1, note_id=i,
                                timing_type=TimingTypes.PERFECT_STAR, combo=i + 1)
                 for i in range(NOTES)],
                ("score", "life", "note_id", "timing_type", "combo")))

    headers = {"Authorization": "Bearer " + make_session_jwt(ACCOUNT, "AppStore")}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://localhost", headers=headers) as client:
        async def raw(path, body=b""):
            response = await client.post(path, content=body)
            assert response.status_code == 200, (path, response.status_code)
            return response.content

        def faults(body):
            """The fault array of a response - the part ``envelope`` refuses to return."""
            return [expand(v) for v in unpack_stream(body)][0]

        async def start(detail_id, live_id, gauge=0):
            return await raw("/api/Lives/StartMusicCourseLive", pack(StartLivePayload(
                live_master_id=live_id, music_course_detail_master_id=detail_id,
                music_course_gauge_type=gauge)))

        # --- a whole attempt: three songs, then the ranking row and its trophies ---------
        held = await trophies()
        for position, (detail_id, live_id) in enumerate(songs, start=1):
            started = envelope(await start(detail_id, live_id))[1]
            assert started, f"no live unit for song {position}"
            playing = await active_live()
            assert playing and int(playing["liveMasterId"]) == live_id, playing
            stage = await run_row()
            assert stage and stage["course"] == course.id_ and stage["next"] == position - 1, stage
            if position == 1:
                assert stage["payment"] in ("free", "ticket", "waived"), stage
            body = await raw("/api/Lives/FinishAndValidate", pack(perfect()))
            if position == len(songs):
                present = envelope(body)[2] or []
        assert await run_row() is None, "the finished attempt left a course run behind"
        print(f"PASS: three songs cleared; the attempt paid {stage['payment']} "
              f"(free attempts first, then the 課題曲参加券: {await tickets_now()} left)")

        saved = await ranking_row()
        assert saved, "no music_course_ranking row for the course that was just cleared"
        assert saved["currentChallengeCount"] == 1, saved
        assert saved["perfectStar"] == NOTES * 3 and saved["miss"] == 0, saved
        assert float(saved["totalAchievementRatePercentRecord"]) == 303.0, saved
        assert saved["bestRecordChallengeCount"] == 1 and saved["bestRecordDate"], saved
        assert saved["hasReceivedReward"] is True, saved
        entries = [item for item in present if item[0] == RANKING_UNION_KEY]
        assert entries, "the ranking row never reached the client in the present blob"
        print(f"PASS: the attempt recorded 3x101.0 = "
              f"{saved['totalAchievementRatePercentRecord']} and {saved['perfectStar']} "
              f"perfect-star notes in music_course_ranking, and that row reached the client "
              f"(union key {RANKING_UNION_KEY})")

        rivals = await field()
        assert len(rivals) == music_courses.RIVAL_COUNT, len(rivals)
        assert all(r["name"].startswith(music_courses.RIVAL_LABEL) for r in rivals), rivals[0]
        rates = [float(r["rate"]) for r in rivals]
        assert rates == music_courses.field_rates(course.id_), "the field is not the seeded one"
        rank = 1 + sum(1 for rate in rates if rate > 303.0)
        band = music_courses.band_for(course.id_, rank)
        granted = await trophies() - held
        expected = {int(thing.thing_id) for thing in band.rewards}
        assert set(granted) == expected and sum(granted.values()) == len(expected), \
            (dict(granted), rank, expected)
        print(f"PASS: rank {rank} against {len(rivals)} {music_courses.RIVAL_LABEL} entrants "
              f"(best {max(rates)}, worst {min(rates)}) paid trophy "
              f"{sorted(expected)} and nothing else")

        # --- a second attempt: a new record, but the band pays once per course -----------
        held = await trophies()      # as they stand after the first payout
        for detail_id, live_id in songs:
            envelope(await start(detail_id, live_id))
            await raw("/api/Lives/FinishAndValidate", pack(perfect()))
        again = await ranking_row()
        assert again["currentChallengeCount"] == 2, again
        assert again["hasReceivedReward"] is True, again
        assert await trophies() == held, "the band paid its trophies twice"
        print(f"PASS: a second cleared attempt raised the challenge count to "
              f"{again['currentChallengeCount']} and paid nothing twice")

        # --- the random courses: the draw upstream never answered -----------------------
        random_course = next(row for row in cache.music_course_master if row.id_ == RANDOM_COURSE)
        random_first = min(random_course.details, key=lambda d: d.set_list_number)
        drawn = envelope(await raw("/api/Lives/SelectMusicCourseRandomMusic", pack(
            SelectMusicCourseRandomMusicPayload(music_course_master_id=RANDOM_COURSE,
                                                music_course_gauge_type=0))))[1]
        assert drawn and drawn[0], f"SelectMusicCourseRandomMusic answered nothing: {drawn}"
        picked = [(int(entry[0]), int(entry[1])) for entry in drawn[0]]
        want = {int(d.set_list_number): (int(d.random_difficulty), int(d.random_level))
                for d in random_course.details}
        assert len(picked) == len(want) == 3, picked
        for number, live_id in picked:
            assert (int(chart(live_id).difficulty), int(chart(live_id).level)) == want[number], \
                (number, live_id, want[number])
        async with app.acquire_db() as conn:
            stored = await conn.conn.fetchrow('SELECT details FROM preservation_course_draw '
                                              'WHERE "userId"=$1 AND "masterId"=$2',
                                              ACCOUNT, RANDOM_COURSE)
        assert stored and [int(d["live_master_id"]) for d in stored["details"]] == \
            [live_id for _, live_id in picked], stored
        drew = ", ".join(f"{songs_of[chart(live).music_master_id]} "
                         f"(難易度{chart(live).difficulty}-{chart(live).level})"
                         for _, live in picked)
        print(f"PASS: {random_course.name} drew {drew}, each inside its own "
              f"random_difficulty/random_level band, and kept the draw")

        # The course start plays what was drawn and nothing else.
        same_band = next(row.id_ for row in cache.live_master
                         if (int(row.difficulty), int(row.level)) == want[picked[0][0]]
                         and row.id_ not in [live_id for _, live_id in picked])
        refused = faults(await start(random_first.id_, same_band))
        assert refused, "a chart that was not drawn was accepted for a random course"
        started = envelope(await start(random_first.id_, picked[0][1]))[1]
        assert started, "the drawn chart was refused"
        playing = await active_live()
        assert playing and int(playing["liveMasterId"]) == picked[0][1], playing
        stage = await run_row()
        assert stage["course"] == RANDOM_COURSE and stage["next"] == 0, stage
        envelope(await raw("/api/Lives/Retire"))
        print(f"PASS: chart {same_band} (same band, not drawn) was refused with "
              f"{refused[0][0]}; chart {picked[0][1]} started, and the draw lives in "
              f"{music_courses.DRAW_TABLE}")

        # --- a fixed weekly course cannot be played with some other chart ---------------
        other = next(row.id_ for row in cache.live_master
                     if row.id_ not in [live_id for _, live_id in songs]
                     and int(row.difficulty) == int(chart(songs[0][1]).difficulty))
        refused = faults(await start(songs[0][0], other))
        assert refused, "a weekly course accepted a chart that is not its own"
        assert await run_row() is None, "a refused start left a course run behind"
        print(f"PASS: {course.name} refused chart {other}; only its own three songs play")

    print("ALL WEEKLY COURSE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
