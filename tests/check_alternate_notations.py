"""Destructive やねうら (April-Fool alternate chart) checks ONLY in a disposable
yumesute_notation_checks DB.

This is the meme-chart mode end to end on a copy of the recovered save: the entry row the
client needs to show the live-screen side button, a run started on a meme chart's own
``LiveMaster`` row, that run's record landing in ``another_notation`` and in the login blob the
client loads, the best-of rule the account's 27 archived rows already show, the two ways a run
is refused, and the permanent ヤネウラ狂奏曲 course whose three details name meme charts and no
songs - including the case where that course run is reported on the alternate-chart route.
test_alternate_notations.py pins the rules against master and reads no database; this check
shows they land in the real schema through the same routes the iPad calls.

Make the scratch database from the live one first - the check runs on a copy of the save,
mutates that copy and nothing else:

    docker compose exec -T db psql -U yumesute -d postgres -c 'CREATE DATABASE yumesute_notation_checks'
    docker compose exec -T db sh -c 'pg_dump -U yumesute -d yumesute | psql -qU yumesute -d yumesute_notation_checks'

Then:  uv run --locked python tests/check_alternate_notations.py
Afterwards:  docker compose exec -T db psql -U yumesute -d postgres -c 'DROP DATABASE yumesute_notation_checks'

The clone needs one precondition reset, since the checks start with nothing in progress:

    docker compose exec -T db psql -U yumesute -d yumesute_notation_checks -c \\
      'DELETE FROM active_live; DELETE FROM preservation_live_context; DELETE FROM preservation_course_run;'

Never point it at a save you intend to keep: it starts and finishes lives and takes one
ウィークリーコース attempt (its free daily attempt, or waived - course 85 asks for no ticket).
Lives are started with ``use_stamina`` false so the clone's stamina stays where it was; what
the real client pays for a run is the master's own cost, charged by ``Lives/Start`` as it does
for any other chart.
"""
import asyncio
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = "yumesute_notation_checks"
ACCOUNT = 5377823715
ENTRY = 21018               # EventMaster row: the ヤネウラ side button on the live screen
MEME_COURSE = 85            # ヤネウラ狂奏曲, MusicCourseType.Special, no end date
NOTATION_UNION = 145        # models/unions.py: AnotherNotation
NOTES = 45                  # score blocks per simulated run
BLOCK_FIELDS = ("score", "life", "note_id", "timing_type", "combo")


def force_scratch_database():
    """Point the app's own config at the scratch database before anything connects.

    The name is replaced unconditionally, so this file can never run against a save.
    """
    from helpers.config import config, database

    config["database"]["database"] = SCRATCH
    database.database = SCRATCH


def chained(blocks):
    """Score blocks whose hash chain the server's verifier accepts."""
    running = 0
    for block in blocks:
        running += sum(int(getattr(block, name)) for name in BLOCK_FIELDS)
        block.hash = running
    return blocks


async def main():
    sys.path[:0] = [str(ROOT), str(ROOT / "tools"), str(ROOT / "vendor/server-of-dreams")]
    import httpx
    from helpers.auth import make_session_jwt
    from helpers.cache import cache, load_master_data
    from helpers.msgpack import from_array, pack
    from helpers.mastermemory import unpack
    from protocol import envelope, expand, unpack_stream

    force_scratch_database()
    import alternate_notations as notations
    import music_courses
    import preservation_dates as dates
    import serve  # the production app, now bound to the scratch database
    from models import BaseScoreBlock, FinishAnotherNotationLivePayload, StartLivePayload
    from models.enums import AchievementRateGrades, ClearLamps, TimingTypes
    from models.keys import KEYS

    app = serve.app
    load_master_data()
    await app.yume_setup()
    async with app.acquire_db() as conn:
        # This run drives the app without its lifespan, so the local tables the live routes
        # need are made here.
        await conn.conn.execute(
            'CREATE TABLE IF NOT EXISTS preservation_live_context '
            '("userId" bigint PRIMARY KEY REFERENCES accounts("userId") ON DELETE CASCADE, '
            'mode text NOT NULL, "masterId" integer NOT NULL)')
        await conn.conn.execute(
            "ALTER TABLE preservation_live_context "
            "ADD COLUMN IF NOT EXISTS extra jsonb NOT NULL DEFAULT '{}'::jsonb")
        await conn.conn.execute(
            'CREATE TABLE IF NOT EXISTS preservation_course_run '
            '("userId" bigint PRIMARY KEY REFERENCES accounts("userId") ON DELETE CASCADE, '
            'data jsonb NOT NULL)')
        await music_courses.ensure_tables(conn)
        in_progress = await conn.conn.fetchval('SELECT count(*) FROM active_live')
        archived = await conn.conn.fetch('SELECT "anotherNotationMasterId","id","clearLamp",'
                                         '"rateGrade","achievementRatePercentRecord" '
                                         'FROM another_notation WHERE "userId"=$1', ACCOUNT)
        party = await conn.conn.fetchrow(
            'SELECT p.id,p.name FROM party p JOIN party_slot s ON s."partyId"=p.id '
            'WHERE p."userId"=$1 GROUP BY p.id,p.name HAVING count(*)>=5 ORDER BY p.id LIMIT 1',
            ACCOUNT)
    assert not in_progress, "the clone already has a live in progress - reset the preconditions"
    assert party, "the save needs a five-actor party to start a live with"
    held = {int(r["anotherNotationMasterId"]): dict(r) for r in archived}
    assert held, f"{SCRATCH} must be a copy of the live save (27 archived alternate records)"
    print(f"PASS: running against {SCRATCH}: {len(held)} archived alternate-chart records, "
          f"best {max(float(r['achievementRatePercentRecord']) for r in held.values()):.4f}%")

    # --- the entry the client needs, in the master it is served --------------------------
    fields = {field: key for key, field, *_ in KEYS["EventMaster"]}
    entry = {row[0]: row for row in unpack(dates.preserved_master()[0])["EventMaster"]}[ENTRY]
    assert entry[fields["is_aside_live_button"]] and int(entry[fields["aside_live_button_type"]]) == 3
    assert entry[fields["end_date"]].seconds == dates.END == entry[fields["force_end_date"]].seconds
    charts = {row.id_: row for row in cache.another_notation_master}

    def chart_of(alt):
        return notations.chart_for(charts[int(alt)])

    assert len(charts) == 112 and all(chart_of(alt) for alt in charts)
    print(f"PASS: EventMaster {ENTRY} (event_type 23, aside_live_button_type 3) is open to 2100, "
          f"all {len(charts)} meme charts resolve to a live row")

    def name_of(music_id):
        return next(music.name for music in cache.music_master if music.id_ == int(music_id))

    def run_payload(alt_id, timing=TimingTypes.PERFECT_STAR, life=1):
        return FinishAnotherNotationLivePayload(
            another_notation_master_id=int(alt_id),
            base_score_blocks=chained([
                BaseScoreBlock(score=101, life=life, note_id=i, timing_type=timing, combo=i + 1)
                for i in range(NOTES)]),
            sense_score_blocks=[], star_act_score_blocks=[])

    async def records():
        async with app.acquire_db() as conn:
            rows = await conn.conn.fetch('SELECT "anotherNotationMasterId","id","clearLamp",'
                                         '"rateGrade","achievementRatePercentRecord" '
                                         'FROM another_notation WHERE "userId"=$1', ACCOUNT)
        return {int(r["anotherNotationMasterId"]): dict(r) for r in rows}

    async def state_of(sql, *args):
        async with app.acquire_db() as conn:
            row = await conn.conn.fetchrow(sql, *args)
        return dict(row) if row else None

    async def balance():
        async with app.acquire_db() as conn:
            items = await conn.conn.fetch('SELECT "itemMasterId",stock FROM item WHERE "userId"=$1',
                                          ACCOUNT)
            currency = await conn.conn.fetchrow('SELECT coin,"freeJewel","paidJewel" FROM currency '
                                                'WHERE "userId"=$1', ACCOUNT)
            trophies = await conn.conn.fetch('SELECT "trophyMasterId" FROM trophy WHERE "userId"=$1',
                                             ACCOUNT)
            missions = await conn.conn.fetch('SELECT "missionMasterId","missionCurrentCount" '
                                             'FROM mission WHERE "userId"=$1', ACCOUNT)
            lives = await conn.conn.fetchrow('SELECT count(*) n, sum("achievementRate") rate, '
                                             'sum("timesCompleted") plays FROM live '
                                             'WHERE "userId"=$1', ACCOUNT)
            player = await conn.conn.fetchval('SELECT "playerRate" FROM user_profile '
                                              'WHERE "userId"=$1', ACCOUNT)
        return (Counter({(1, row["itemMasterId"]): row["stock"] for row in items}
                        | {(12, 0): currency["coin"], (13, 0): currency["freeJewel"],
                           (14, 0): currency["paidJewel"]}),
                Counter(row["trophyMasterId"] for row in trophies),
                {int(row["missionMasterId"]): int(row["missionCurrentCount"]) for row in missions},
                (int(lives["n"]), float(lives["rate"] or 0), int(lives["plays"] or 0)),
                float(player or 0.0))

    fresh = next(alt for alt in sorted(charts) if alt not in held)
    best = max(held, key=lambda alt: float(held[alt]["achievementRatePercentRecord"]))
    headers = {"Authorization": "Bearer " + make_session_jwt(ACCOUNT, "AppStore")}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://localhost", headers=headers) as client:
        async def raw(path, body=b""):
            response = await client.post(path, content=body)
            assert response.status_code == 200, (path, response.status_code)
            return response.content

        def faults(body):
            return [expand(value) for value in unpack_stream(body)][0]

        def outcome(body):
            return from_array("FinishLiveResult", envelope(body)[1])

        async def start(live_id):
            started = envelope(await raw("/api/Lives/Start", pack(StartLivePayload(
                party_id=int(party["id"]), live_master_id=int(live_id), use_stamina=False))))
            assert started[1] and not started[0], started[0]

        async def finish(payload):
            body = await raw("/api/Lives/FinishAnotherNotationLive", pack(payload))
            assert not faults(body), faults(body)
            return body

        # --- the route is the preservation one, not the vendor's empty stub -------------
        probe = await raw("/api/Lives/FinishAnotherNotationLive", pack(run_payload(fresh)))
        assert any("No active live" in str(value) for value in faults(probe)), faults(probe)
        assert fresh not in await records()
        print("PASS: the finish route answers with the preservation implementation - the pinned "
              "vendor stub returns an empty result and writes nothing")

        # --- a first clean run on a chart with no record ---------------------------------
        before = await balance()
        alt = charts[fresh]
        await start(chart_of(fresh).id_)
        playing = await state_of('SELECT * FROM active_live WHERE "userId"=$1', ACCOUNT)
        assert playing and int(playing["liveMasterId"]) == int(chart_of(fresh).id_)
        assert await state_of('SELECT 1 FROM active_live WHERE "userId"=$1', ACCOUNT)
        mode = await state_of('SELECT mode FROM preservation_live_context WHERE "userId"=$1',
                              ACCOUNT)
        assert mode and mode["mode"] == "normal", mode
        result = outcome(await finish(run_payload(fresh)))
        written = (await records()).get(fresh)
        assert written and int(written["clearLamp"]) == int(ClearLamps.AllPerfect)
        assert written["achievementRatePercentRecord"] == "101.0000"
        assert int(written["rateGrade"]) == int(AchievementRateGrades.SSS)
        assert int(result.clear_lamp) == int(ClearLamps.AllPerfect)
        assert int(result.before_clear_lamp) == int(ClearLamps.None_)
        assert result.is_high_score
        assert await state_of('SELECT 1 FROM active_live WHERE "userId"=$1', ACCOUNT) is None
        assert await state_of('SELECT 1 FROM preservation_live_context WHERE "userId"=$1',
                              ACCOUNT) is None
        print(f"PASS: {name_of(alt.music_master_id)} / 難易度{alt.difficulty}-{alt.level} "
              f"(chart {fresh}, type {alt.another_notation_type}) started on live "
              f"{chart_of(fresh).id_} and wrote clearLamp {written['clearLamp']}, "
              f"rateGrade {written['rateGrade']}, record "
              f"{written['achievementRatePercentRecord']}")

        # --- the best-of rule, both directions ------------------------------------------
        await start(chart_of(fresh).id_)
        first = (await records())[fresh]
        result = outcome(await finish(run_payload(fresh, timing=TimingTypes.GREAT)))
        after = (await records())[fresh]
        assert after["id"] == first["id"] and after["achievementRatePercentRecord"] == "101.0000"
        assert int(after["clearLamp"]) == int(ClearLamps.AllPerfect)
        assert int(result.before_clear_lamp) == int(ClearLamps.AllPerfect)
        assert not result.is_high_score
        kept = held[best]
        await start(chart_of(best).id_)
        await finish(run_payload(best, timing=TimingTypes.PERFECT))
        same = (await records())[best]
        assert same["achievementRatePercentRecord"] == kept["achievementRatePercentRecord"]
        assert int(same["clearLamp"]) == int(kept["clearLamp"])
        assert int(same["id"]) == int(kept["id"]), "the archived row must be updated, not added"
        print(f"PASS: weaker runs left both records in place - chart {fresh} kept 101.0000, and "
              f"chart {best} kept {kept['achievementRatePercentRecord']} in its archived row "
              f"id {kept['id']}")

        # --- two ways a run is refused --------------------------------------------------
        wrong = next(alt for alt in sorted(charts)
                     if alt not in held and int(charts[alt].music_master_id)
                     != int(charts[fresh].music_master_id))
        await start(chart_of(fresh).id_)
        refused = await raw("/api/Lives/FinishAnotherNotationLive", pack(run_payload(wrong)))
        assert faults(refused), "a record for another song's chart was accepted"
        assert wrong not in await records(), "the refused run still wrote a record"
        assert await state_of('SELECT 1 FROM active_live WHERE "userId"=$1', ACCOUNT), \
            "a refused run should leave its live to retire"
        await raw("/api/Lives/Retire")
        await start(chart_of(fresh).id_)
        tampered = run_payload(fresh, timing=TimingTypes.PERFECT)
        tampered.base_score_blocks[10].score += 500_000
        body = await raw("/api/Lives/FinishAnotherNotationLive", pack(tampered))
        assert any("InvalidScoreBlockHash" in str(value) for value in faults(body)), faults(body)
        assert (await records())[fresh]["achievementRatePercentRecord"] == "101.0000"
        await raw("/api/Lives/Retire")
        print(f"PASS: a run reporting another song's chart and a run whose block hash chain does "
              f"not reproduce are both refused, with the record and the live untouched")

        # --- what the mode deliberately does not touch ----------------------------------
        untouched = await balance()
        assert untouched[0] == before[0], "an alternate run granted or spent something"
        assert untouched[1] == before[1], "an alternate run paid a trophy"
        assert untouched[2] == before[2], "an alternate run moved a mission counter"
        assert untouched[3] == before[3], "an alternate run wrote the song's own live row"
        assert untouched[4] == before[4], "an alternate run moved the player's live rate"
        print(f"PASS: across four accepted alternate runs the items, currency, trophies, "
              f"{len(before[2])} mission counters, {before[3][0]} live rows (total rate "
              f"{before[3][1]:.2f}, player rate {before[4]:.2f}) never moved")

        # --- the client sees the records on login ---------------------------------------
        listing = envelope((await client.get("/api/data/user")).content)[1]
        carried = [item for item in listing if isinstance(item, list) and item
                   and item[0] == NOTATION_UNION]
        assert len(carried) == len(await records()), (len(carried), len(await records()))
        print(f"PASS: /api/data/user carries all {len(carried)} alternate records as "
              f"IDataObject union key {NOTATION_UNION}")

        # --- the permanent ヤネウラ狂奏曲 course ----------------------------------------
        course = next(row for row in cache.music_course_master if row.id_ == MEME_COURSE)
        details = sorted(course.details, key=lambda detail: detail.set_list_number)
        assert all(detail.another_notation_master_id and not detail.live_master_id
                   for detail in details)
        none_yet = await state_of('SELECT 1 FROM music_course_ranking WHERE "userId"=$1 AND '
                                  '"musicCourseMasterId"=$2', ACCOUNT, MEME_COURSE)
        for position, detail in enumerate(details, start=1):
            chart = notations.chart_for(notations.notation(detail.another_notation_master_id))
            body = pack(StartLivePayload(live_master_id=int(chart.id_),
                                         music_course_detail_master_id=int(detail.id_),
                                         music_course_gauge_type=0))
            started = envelope(await raw("/api/Lives/StartMusicCourseLive", body))
            assert started[1] and not started[0], (detail.id_, started[0])
            stage = await state_of('SELECT data FROM preservation_course_run WHERE "userId"=$1',
                                   ACCOUNT)
            assert stage and stage["data"]["course"] == MEME_COURSE \
                and stage["data"]["next"] == position - 1, stage
            paid = stage["data"]["payment"]
            payload = run_payload(detail.another_notation_master_id)
            # song 1 is reported on the alternate-chart route, the other two on the normal one:
            # both have to settle the same course stage.
            if position == 1:
                body = await raw("/api/Lives/FinishAnotherNotationLive", pack(payload))
            else:
                body = await raw("/api/Lives/FinishAndValidate",
                                 pack(notations.as_live_payload(payload)))
            assert not faults(body), (position, faults(body))
            stage = await state_of('SELECT data FROM preservation_course_run WHERE "userId"=$1',
                                   ACCOUNT)
            if position < len(details):
                assert stage and stage["data"]["next"] == position, "the course stage did not advance"
        assert await state_of('SELECT 1 FROM preservation_course_run WHERE "userId"=$1',
                              ACCOUNT) is None
        assert none_yet is None and await state_of(
            'SELECT 1 FROM music_course_ranking WHERE "userId"=$1 AND "musicCourseMasterId"=$2',
            ACCOUNT, MEME_COURSE) is None, "the special course has no rank bands to pay"
        assert int(course.required_amount) == 0 and course.required_item_master_id is None
        assert paid in ('unlimited', 'free', 'waived'), paid
        after_course = await balance()
        assert after_course[1] == untouched[1], "course 85 pays no trophy band"
        assert after_course[3][2] == untouched[3][2] + 3, \
            "the course stages belong to the song's own live row - that is the normal route"
        print(f"PASS: {course.name} played its three meme charts (details "
              f"{[int(d.id_) for d in details]} → charts "
              f"{[int(d.another_notation_master_id) for d in details]}), paid {paid}, settled "
              f"from both finish routes, and asked for no ranking row, ticket or trophy; the "
              f"three runs went to the live rows as a normal course run does")


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\nAll やねうら checks passed against {SCRATCH}.")
