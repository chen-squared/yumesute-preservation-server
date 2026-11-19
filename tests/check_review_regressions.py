"""Review regressions through real routes, ONLY on a disposable save clone.

Pass --database yumesute_review_checks_<suffix>. The database must already contain a
prepared installation's copied save. Each case rolls its data changes back; startup
may create overlay tables in that clone. Never pass the live database.
"""
import argparse
import asyncio
import contextlib
import importlib.util
import io
import sys
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools'), str(ROOT / 'vendor/server-of-dreams')]


def finish_payload(timing, life=1):
    from models import BaseScoreBlock, FinishLivePayload
    blocks, running = [], 0
    for i in range(20):
        b = BaseScoreBlock(score=101, life=life, note_id=i, timing_type=timing, combo=i + 1)
        running += sum(int(getattr(b, name)) for name in ('score', 'life', 'note_id', 'timing_type', 'combo'))
        b.hash = running
        blocks.append(b)
    return FinishLivePayload(base_score_blocks=blocks)


async def main(name):
    if not name.startswith('yumesute_review_checks_') or not name.isidentifier():
        raise SystemExit('Only a disposable yumesute_review_checks_<suffix> database is allowed')
    from helpers.config import config, database
    config['database']['database'] = database.database = name
    import httpx
    import serve
    import alternate_notations
    import gameplay
    import league_schedule
    import missions
    import music_courses
    import course_schedule
    import progression
    from helpers.auth import make_session_jwt
    from helpers.cache import cache
    from helpers.live_rate import chart_live_rate
    from helpers.msgpack import pack
    from models import (FinishAnotherNotationLivePayload, PurchaseItemPayload, StartLivePayload,
                        StartTripleCastLivePayload, TripleCastPartyScore)
    from models.keys import KEYS
    from models.enums import TimingTypes
    from protocol import envelope, expand, unpack_stream

    app = serve.app
    async with app.router.lifespan_context(app):
        async with app.acquire_db() as conn:
            assert await conn.conn.fetchval('SELECT current_database()') == name
            uid = await conn.conn.fetchval(
                'SELECT "userId" FROM "character" GROUP BY "userId" ORDER BY count(*) DESC LIMIT 1')
            assert uid is not None, 'Copy a prepared save into the disposable database first'
            party = await conn.conn.fetchval('SELECT "partyId" FROM party_slot WHERE "userId"=$1 '
                                             'GROUP BY "partyId" HAVING count(*) >= 5 LIMIT 1', uid)
            assert party is not None, 'The copied save needs a five-slot party'
            token = make_session_jwt(uid, 'AppStore')
            original_acquire = app.acquire_db

            @asynccontextmanager
            async def bound():
                yield conn

            # Requests run on this connection, nested inside each case's rollback.
            app.acquire_db = bound
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url='http://localhost',
                                             headers={'Authorization': f'Bearer {token}'}) as client:
                    async def post(path, payload=None, valid=True):
                        r = await client.post(path, content=pack(payload) if payload is not None else b'')
                        assert r.status_code == 200, (path, r.status_code)
                        parts = [expand(v) for v in unpack_stream(r.content)]
                        assert bool(parts[0]) != valid, (path, parts[0])
                        return parts if not valid else envelope(r.content)

                    async def reset_live():
                        for table in ('active_live', 'preservation_live_context', 'preservation_course_run'):
                            await conn.conn.execute(f'DELETE FROM {table} WHERE "userId"=$1', uid)

                    async def compatible_exports():
                        from helpers.user_data import user_data
                        before = pack(await user_data(app, uid))
                        expected = pack(await serve.export_user_data(app, uid))
                        spec = importlib.util.spec_from_file_location(
                            'export_local_account', ROOT / 'local/export_local_account.py')
                        cli = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(cli)
                        with tempfile.TemporaryDirectory() as folder:
                            # Exercise the actual command-line implementation and its
                            # independent read-only connection, not another copy of the helper.
                            with contextlib.redirect_stdout(io.StringIO()):
                                await asyncio.to_thread(cli.run, Path(folder), uid)
                            files = list(Path(folder).glob('*.zip'))
                            assert len(files) == 1
                            with zipfile.ZipFile(files[0]) as archive:
                                actual = envelope(archive.read('user-data.response.bin'))[1]
                            assert pack(actual) == expected, 'CLI export lost captured wire layout'
                        assert pack(await user_data(app, uid)) == before, 'Export changed local progress'

                    async def effect_schedules():
                        from helpers.live import build_live_unit, build_live_time_event
                        from helpers.msgpack import to_wire
                        slot = await conn.conn.fetchrow('SELECT * FROM party_slot WHERE "userId"=$1 '
                                                        'AND "partyId"=$2 AND "position"=1', uid, party)
                        assert slot and slot['characterId']
                        await conn.conn.execute('UPDATE "character" SET "characterMasterId"=141020, '
                                                '"talentStage"=0 WHERE "userId"=$1 AND id=$2',
                                                uid, slot['characterId'])
                        await conn.conn.execute('UPDATE party_slot SET "posterId"=NULL,"accessoryId"=NULL '
                                                'WHERE "userId"=$1 AND id=$2', uid, slot['id'])
                        chart = next(m for m in cache.live_master if m.difficulty == 3)
                        unit, _ = await build_live_unit(conn, uid, party, chart.id_)
                        sense = next(s for s in unit.possible_senses if s.actor_id == slot['characterId'])
                        events = [e for e in unit.time_events.values() if sense.id_ in e.sense_ids]
                        assert events and all(e.acquirable_lights == sense.acquirable_lights for e in events)
                        schedule = await build_live_time_event(conn, uid, party, chart.music_master_id)
                        assert {t.timing_seconds: to_wire(t.event) for t in schedule.timings} == {
                            second: to_wire(event) for second, event in unit.time_events.items()}

                    async def claim_after_boundary():
                        # Yesterday's completed daily cannot be claimed as today's reward.
                        s = gameplay.State(conn, uid)
                        await missions.rotate(s)
                        m = gameplay.master('mission_master', missions.FREE_PACK)
                        stage = missions.current_stage(m, time.time_ns() // 1000)
                        row = await s.one('Mission', missionMasterId=m.id_)
                        await s.update('Mission', row, missionCurrentCount=stage.stage_goal_value,
                                       isCleared=True, isRewardReceived=False)
                        await conn.conn.execute('UPDATE preservation_mission_cycle SET "cycleStartAt"=0 '
                                                'WHERE "userId"=$1', uid)
                        result = await post(f'/api/Missions/{m.id_}/receiveCurrentRewards')
                        assert result[1] == [], 'An expired daily paid its old reward'
                        row = await conn.conn.fetchrow('SELECT * FROM mission WHERE "userId"=$1 '
                                                       'AND "missionMasterId"=$2', uid, m.id_)
                        assert not row['isCleared'] and row['missionCurrentCount'] == 0
                        await conn.conn.execute('UPDATE jewel_shop SET "rePurchaseDate"=0 WHERE "userId"=$1 '
                                                'AND "jewelShopItemMasterId"=4101', uid)
                        await post('/api/Shops/Purchase', PurchaseItemPayload(m_jewel_shop_item_id=4101))
                        # The next full refresh must keep the purchase in today's mission.
                        await progression.refresh_daily(app, uid)
                        row = await conn.conn.fetchrow('SELECT * FROM mission WHERE "userId"=$1 '
                                                       'AND "missionMasterId"=$2', uid, m.id_)
                        assert row['isCleared'] and row['missionCurrentCount'] == 1

                    async def imported_rank_cap():
                        await conn.conn.execute('DELETE FROM rank_cap_release WHERE user_id=$1', uid)
                        await conn.conn.execute('UPDATE "user" SET "playerRankLimit"=269 WHERE "userId"=$1', uid)
                        await conn.conn.execute('DELETE FROM audition_clear WHERE "userId"=$1', uid)
                        s = gameplay.State(conn, uid)
                        for audition in cache.audition_master[:220]:
                            await s.insert('AuditionClear', auditionMasterId=audition.id_, clearPhase=2,
                                           skipClearPhase=0, auditionClearPartyId=0)
                        assert await gameplay.grow_rank_cap(s) == 1
                        assert (await s.one('User'))['playerRankLimit'] == 270

                    async def failed_rating():
                        await reset_live()
                        chart = next(row for row in cache.live_master
                                     if chart_live_rate(row.id_, 101.0) is not None)
                        await conn.conn.execute('DELETE FROM live WHERE "userId"=$1 AND "liveMasterId"=$2',
                                                uid, chart.id_)
                        for timing, life in ((TimingTypes.PERFECT_STAR, 0), (TimingTypes.GREAT, 1)):
                            await post('/api/Lives/Start', StartLivePayload(
                                party_id=party, live_master_id=chart.id_))
                            await post('/api/Lives/FinishAndValidate', finish_payload(timing, life))
                        row = await conn.conn.fetchrow('SELECT * FROM live WHERE "userId"=$1 '
                                                       'AND "liveMasterId"=$2', uid, chart.id_)
                        assert row['achievementRate'] == 101.0
                        assert row['notationRate'] == chart_live_rate(chart.id_, 80.0), \
                            'The failed 101% was promoted to rating by the subsequent 80% clear'

                    async def alternate_course():
                        await reset_live()
                        course = gameplay.master('music_course_master', 85)
                        before = await conn.conn.fetchval('SELECT json_agg(l ORDER BY id)::text '
                                                         'FROM live l WHERE "userId"=$1', uid)
                        rating = await conn.conn.fetchval('SELECT "playerRate" FROM user_profile '
                                                         'WHERE "userId"=$1', uid)
                        for index, detail in enumerate(sorted(course.details, key=lambda d: d.set_list_number)):
                            chart = alternate_notations.chart_for(alternate_notations.notation(
                                detail.another_notation_master_id))
                            await post('/api/Lives/StartMusicCourseLive', StartLivePayload(
                                live_master_id=chart.id_, music_course_detail_master_id=detail.id_))
                            payload = finish_payload(TimingTypes.PERFECT_STAR)
                            if index == 0:
                                wrong = FinishAnotherNotationLivePayload(
                                    another_notation_master_id=course.details[-1].another_notation_master_id,
                                    base_score_blocks=payload.base_score_blocks)
                                await post('/api/Lives/FinishAnotherNotationLive', wrong, valid=False)
                                payload = FinishAnotherNotationLivePayload(
                                    another_notation_master_id=detail.another_notation_master_id,
                                    base_score_blocks=payload.base_score_blocks)
                                await post('/api/Lives/FinishAnotherNotationLive', payload)
                            else:
                                await post('/api/Lives/FinishAndValidate', payload)
                        after = await conn.conn.fetchval('SELECT json_agg(l ORDER BY id)::text '
                                                        'FROM live l WHERE "userId"=$1', uid)
                        assert before == after, 'The alternate course wrote normal chart records'
                        assert rating == await conn.conn.fetchval('SELECT "playerRate" FROM user_profile '
                                                                   'WHERE "userId"=$1', uid)
                        assert not await conn.conn.fetchval('SELECT 1 FROM preservation_course_run '
                                                           'WHERE "userId"=$1', uid)

                    async def closed_league():
                        await reset_live()
                        window = league_schedule.anchor('league')
                        chart = next(c for c in cache.live_master if c.music_master_id == window.music)
                        await post('/api/Lives/Start', StartLivePayload(
                            party_id=party, live_master_id=chart.id_, league_master_id=window.id), valid=False)
                        assert not await conn.conn.fetchval('SELECT 1 FROM active_live WHERE "userId"=$1', uid)

                    async def rank_up_stamina():
                        m = gameplay.master('player_rank_master', 100, field='rank')
                        for held in (1200, 2900, 3000):
                            await conn.conn.execute('UPDATE "user" SET "playerRank"=100,"playerRankLimit"=150,'
                                '"currentRankPoint"=$2,"currentStamina"=$3,"maxStaminaRestoredAt"=0 '
                                'WHERE "userId"=$1', uid, m.point_to_level_up, held)
                            await progression.rank_xp(gameplay.State(conn, uid), 0)
                            row = await conn.conn.fetchrow('SELECT "playerRank","currentStamina" FROM "user" '
                                                           'WHERE "userId"=$1', uid)
                            expected = min(3000, held + progression.max_stamina(101))
                            assert row['playerRank'] == 101 and row['currentStamina'] == expected

                    async def weekly_rewards():
                        await reset_live()
                        live = course_schedule.live()
                        assert live, 'Run this check during the weekly course play period'
                        course = gameplay.master('music_course_master', live[0].id)
                        for table in ('music_course_ranking', 'music_course'):
                            await conn.conn.execute(f'DELETE FROM {table} WHERE "userId"=$1 '
                                                    'AND "musicCourseMasterId"=$2', uid, course.id_)
                        async def trophies():
                            return await conn.conn.fetchval('SELECT json_agg(t ORDER BY "id")::text '
                                                            'FROM trophy t WHERE "userId"=$1', uid)
                        held = await trophies()
                        for timing in (TimingTypes.GREAT, TimingTypes.PERFECT_STAR, TimingTypes.GREAT):
                            for d in sorted(course.details, key=lambda r: r.set_list_number):
                                await post('/api/Lives/StartMusicCourseLive', StartLivePayload(
                                    live_master_id=d.live_master_id, music_course_detail_master_id=d.id_))
                                await post('/api/Lives/FinishAndValidate', finish_payload(timing))
                            assert await trophies() == held, 'Weekly trophies paid before the period ended'
                        saved = await conn.conn.fetchrow('SELECT * FROM music_course_ranking WHERE "userId"=$1 '
                                                        'AND "musicCourseMasterId"=$2', uid, course.id_)
                        assert saved['currentChallengeCount'] == 3
                        assert saved['perfectStar'] == 60 and saved['great'] == 0
                        assert float(saved['totalAchievementRatePercentRecord']) == 303.0
                        assert saved['bestRecordDate'] > 1_000_000_000_000_000
                        assert not saved['hasReceivedReward']
                        top = (await post(f'/api/Lives/GetCourseTopRanking/{course.id_}'))[1]
                        assert top[0][0] == 1 and top[0][1] == uid
                        near = (await post(f'/api/Lives/GetCourseNearRanking/{course.id_}'))[1]
                        assert any(r[1] == uid for r in near)
                        end = course_schedule._seconds(course.end_date)
                        await music_courses.pay_closed(gameplay.State(conn, uid), now=end)
                        paid = await trophies()
                        assert paid != held, 'The closed course never paid its trophy'
                        await music_courses.pay_closed(gameplay.State(conn, uid), now=end + 1)
                        assert await trophies() == paid, 'The course paid twice'

                    async def league_master_and_difficulties():
                        from helpers.mastermemory import unpack
                        r = await client.get('/master-data/production/review.db')
                        assert r.status_code == 200
                        tables = unpack(r.content)
                        for kind, table, path in (
                                ('league', 'LeagueMaster', '/api/Leagues/TopMenuInformation'),
                                ('triple_cast', 'TripleCastMaster', '/api/TripleCast/TopMenuInformation')):
                            fields = {field: key for key, field, *_ in KEYS[table]}
                            now = time.time()
                            current = [row for row in tables[table]
                                if row[fields['display_start_at']].seconds <= now < row[fields['display_end_at']].seconds]
                            assert len(current) == 1, 'Client sees overlapping league windows'
                            menu = (await post(path))[1]
                            music = current[0][fields['music_master_id']]
                            assert menu[3] == music
                            assert current[0][fields['max_difficulty']] == 5
                            jacket = await client.get(f'/production/2d-assets/iOS/1.96.0/jacket_assets_jacket/{music}.bundle')
                            assert jacket.status_code == 200 and len(jacket.content) > 1000
                            window = league_schedule.live(kind)
                            for difficulty in (4, 5):
                                await reset_live()
                                chart = next(c for c in cache.live_master
                                             if c.music_master_id == music and int(c.difficulty) == difficulty)
                                plays = await conn.conn.fetchval('SELECT "timesCompleted" FROM live '
                                    'WHERE "userId"=$1 AND "liveMasterId"=$2', uid, chart.id_) or 0
                                rating = await conn.conn.fetchval('SELECT "playerRate" FROM user_profile WHERE "userId"=$1', uid)
                                stars = await conn.conn.fetchval('SELECT coalesce(sum(point),0) FROM sp_rate WHERE "userId"=$1', uid)
                                asset = await client.get(f'/production/Notations/{music}/{difficulty}.enc')
                                assert asset.status_code == 200 and len(asset.content) > 0
                                if kind == 'league':
                                    await post('/api/Lives/Start', StartLivePayload(
                                        party_id=party, live_master_id=chart.id_, league_master_id=window.id))
                                else:
                                    result = (await post('/api/Lives/StartTripleCastLive',
                                        StartTripleCastLivePayload(m_live_id=chart.id_, m_triple_cast_id=window.id)))[1]
                                    assert len(result[0]) == 3
                                    for unit in result[0]:
                                        assert all(len(event) == len(KEYS['TimingEvent']) for event in unit[1].values())
                                active = await conn.conn.fetchrow('SELECT * FROM active_live WHERE "userId"=$1', uid)
                                assert active['liveMasterId'] == chart.id_ and not active['staminaSpent']
                                payload = finish_payload(TimingTypes.PERFECT_STAR)
                                if kind == 'triple_cast':
                                    payload.triple_cast_party_scores = [TripleCastPartyScore(order=n, score=score)
                                        for n, score in enumerate((674, 673, 673), start=1)]
                                await post('/api/Lives/FinishAndValidate', payload)
                                saved = await conn.conn.fetchrow('SELECT * FROM live WHERE "userId"=$1 '
                                                                'AND "liveMasterId"=$2', uid, chart.id_)
                                assert saved['timesCompleted'] == plays + 1
                                assert saved['achievementRate'] == 101.0 and saved['clearLamp'] == 6
                                assert rating == await conn.conn.fetchval('SELECT "playerRate" FROM user_profile WHERE "userId"=$1', uid)
                                assert stars == await conn.conn.fetchval('SELECT coalesce(sum(point),0) FROM sp_rate WHERE "userId"=$1', uid)
                            if kind == 'triple_cast':
                                # A weaker total with one stronger party must not replace a
                                # single party in the previous best run's three-party snapshot.
                                await post('/api/Lives/StartTripleCastLive', StartTripleCastLivePayload(
                                    m_live_id=chart.id_, m_triple_cast_id=window.id))
                                payload = finish_payload(TimingTypes.PERFECT_STAR)
                                payload.triple_cast_party_scores = [TripleCastPartyScore(order=n, score=score)
                                    for n, score in enumerate((1500, 100, 100), start=1)]
                                await post('/api/Lives/FinishAndValidate', payload)
                                entries = (await post(f'/api/TripleCast/GroupRanking/{window.id}'))[1]
                                fields = {f: k for k, f, *_ in KEYS['TripleCastPartyAndRankingResult']}
                                own = next(r for r in entries if r[fields['user_id']] == str(uid))
                                party_fields = {f: k for k, f, *_ in KEYS['TripleCastPartyResult']}
                                parties = own[fields['parties']]
                                assert [p[party_fields['order']] for p in parties] == [1, 2, 3]
                                assert [p[party_fields['score']] for p in parties] == [674, 673, 673]
                                assert sum(p[party_fields['score']] for p in parties) == own[fields['best_score']]

                    for case in (compatible_exports, effect_schedules,
                                 claim_after_boundary, imported_rank_cap, failed_rating,
                                 alternate_course, closed_league, rank_up_stamina, weekly_rewards,
                                 league_master_and_difficulties):
                        tx = conn.transaction()
                        await tx.start()
                        try:
                            await case()
                            print(f'PASS: {case.__name__}')
                        finally:
                            await tx.rollback()
            finally:
                app.acquire_db = original_acquire


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    asyncio.run(main(parser.parse_args().database))
