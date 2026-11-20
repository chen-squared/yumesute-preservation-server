"""One-song challenge checks, ONLY on a disposable yumesute_review_checks_<suffix> clone."""
import argparse
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools'), str(ROOT / 'vendor/server-of-dreams')]


def result_payload(life=1):
    from models import BaseScoreBlock, FinishLivePayload
    from models.enums import TimingTypes
    blocks, running = [], 0
    for i in range(20):
        block = BaseScoreBlock(score=101, life=life, note_id=i, timing_type=TimingTypes.PERFECT_STAR, combo=i + 1)
        running += sum(int(getattr(block, f)) for f in ('score', 'life', 'note_id', 'timing_type', 'combo'))
        block.hash = running
        blocks.append(block)
    return FinishLivePayload(base_score_blocks=blocks)


async def main(name):
    if not name.startswith('yumesute_review_checks_') or not name.isidentifier():
        raise SystemExit('Only a disposable yumesute_review_checks_<suffix> database is allowed')
    from helpers.config import config, database
    config['database']['database'] = database.database = name
    import httpx
    import serve
    import song_challenge as challenge
    from core import YumeApp
    from gameplay import State
    from helpers.auth import make_session_jwt
    from helpers.cache import cache
    from helpers.msgpack import pack
    from models import EditBookmarkPayload, StartLessonPayload, StartLivePayload
    from models.unions import IDATA_OBJECT_KEY
    from protocol import envelope, expand, unpack_stream

    app = serve.app
    async with app.router.lifespan_context(app):
        async with app.acquire_db() as c:
            assert await c.conn.fetchval('SELECT current_database()') == name
            uid = await c.conn.fetchval('SELECT "userId" FROM "character" GROUP BY "userId" ORDER BY count(*) DESC LIMIT 1')
            other = await c.conn.fetchval('SELECT "userId" FROM accounts WHERE "userId"<>$1 LIMIT 1', uid)
            assert other is not None, 'Copy a prepared save with two accounts'
            party = await c.conn.fetchval('SELECT "partyId" FROM party_slot WHERE "userId"=$1 '
                                         'GROUP BY "partyId" HAVING count(*)>=5 LIMIT 1', uid)
            assert party is not None
            lesson_base = await c.conn.fetchval('SELECT "characterBaseMasterId" FROM character_base '
                                               'WHERE "userId"=$1 ORDER BY "characterBaseMasterId" LIMIT 1', uid)
            for table in ('active_live', 'preservation_live_context', 'preservation_course_run'):
                await c.conn.execute(f'DELETE FROM {table} WHERE "userId"=$1', uid)
            await c.conn.execute(f'DELETE FROM {challenge.TABLE} WHERE "userId"=ANY($1::bigint[])', [uid, other])
            await c.conn.execute('DELETE FROM music_bookmark WHERE "userId"=ANY($1::bigint[])', [uid, other])

        async def state_action(fn, user=uid):
            async with app.acquire_db() as c, c.transaction():
                await c.conn.execute('SELECT pg_advisory_xact_lock($1::bigint)', user)
                return await fn(State(c, user))

        async def current():
            async with app.acquire_db() as c:
                return await challenge.current(c, uid)

        async def markers(user=uid):
            async with app.acquire_db() as c:
                return {r['musicMasterId']: r['musicBookmarkFlag'] for r in await c.conn.fetch(
                    'SELECT * FROM music_bookmark WHERE "userId"=$1', user)}

        pool = await state_action(challenge.candidates)
        assert len(pool) > 10
        seed_favorite = pool[0]
        async with app.acquire_db() as c:
            await c.conn.execute('INSERT INTO music_bookmark ("userId","musicMasterId","musicBookmarkFlag") VALUES($1,$2,3)', uid, seed_favorite)
        await state_action(challenge.enable)
        initial = await current()
        assert initial['enabled'] and initial['bookmarkFlag'] == 4
        assert initial['musicMasterId'] in pool and initial['completedCount'] == 0
        assert (await markers())[seed_favorite] & 3 == 3, 'Enabling overwrote personal favorites'
        assert [m for m, f in (await markers()).items() if f & 4] == [initial['musicMasterId']]
        for _ in range(3):
            await state_action(challenge.enable)
        assert (await current())['musicMasterId'] == initial['musicMasterId']
        print('PASS: an empty bookmark group holds one owned song; personal favorites and the pending song are retained')

        token = make_session_jwt(uid, 'AppStore')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                    base_url='http://localhost', headers={'Authorization': f'Bearer {token}'}) as client:
            async def post(path, payload=None, valid=True, status=200):
                r = await client.post(path, content=pack(payload) if payload is not None else b'')
                assert r.status_code == status, (path, r.status_code, r.text[:100] if r.status_code != 200 else '')
                if status != 200:
                    return r
                parts = [expand(v) for v in unpack_stream(r.content)]
                assert bool(parts[0]) != valid, (path, parts[0])
                return parts

            async def start(music, difficulty=4, auto=False):
                chart = next(m for m in cache.live_master if m.music_master_id == music and int(m.difficulty) == difficulty)
                return await post('/api/Lives/Start', StartLivePayload(
                    party_id=party, live_master_id=chart.id_, is_auto_play=auto))

            async def start_lesson(music):
                chart = next(m for m in cache.live_master if m.music_master_id == music and int(m.difficulty) == 4)
                return await post('/api/Lives/StartLesson', StartLessonPayload(
                    character_base_master_id=lesson_base, live_master_id=chart.id_))

            async def edit(music, flag):
                return await post('/api/Lives/Music/EditBookmark', EditBookmarkPayload(music_master_id=music, bookmark_flag=flag))

            pending = initial['musicMasterId']
            elsewhere = next(m for m in pool if m != pending)
            await edit(elsewhere, 7)
            assert (await markers())[elsewhere] == 3
            await edit(pending, 1)
            assert (await markers())[pending] == 5
            unused = next(m for m in pool if m not in (pending, elsewhere, seed_favorite))
            refused = await edit(unused, 4)
            assert refused[2] == [[IDATA_OBJECT_KEY['MusicBookmark'], [unused, 0]]]
            assert (await markers()).get(unused, 0) == 0
            assert [m for m, f in (await markers()).items() if f & 4] == [pending]
            print('PASS: bookmark edits can change personal groups, but cannot remove or replace the pending challenge song')

            for _ in range(2):
                data = envelope((await client.get('/api/data/user')).content)[1]
                shown = [v[1][0] for v in data if v and v[0] == IDATA_OBJECT_KEY['MusicBookmark'] and v[1][1] & 4]
                assert shown == [pending] and (await current())['generation'] == 1
            check = YumeApp(config=database)
            await check.yume_setup()
            try:
                async with check.acquire_db() as c:
                    restored = await challenge.current(c, uid)
                    assert restored['musicMasterId'] == pending and restored['generation'] == initial['generation']
            finally:
                await check.close()
            print('PASS: user-data reload and a new database pool retain the same pending song')

            await start(pending)
            await post('/api/Lives/Retire')
            assert (await current())['generation'] == 1
            await start(pending, auto=True)
            await post('/api/Lives/FinishAndValidate', result_payload())
            assert (await current())['generation'] == 1
            await start(elsewhere)
            await post('/api/Lives/FinishAndValidate', result_payload())
            assert (await current())['generation'] == 1
            print('PASS: retirement, auto play and other songs do not reroll the challenge')

            await start_lesson(elsewhere)
            await post('/api/Lives/FinishAndValidate', result_payload())
            await start_lesson(pending)
            await post('/api/Lives/Retire')
            assert (await current())['generation'] == 1
            await start_lesson(pending)
            assert (await current())['generation'] == 1
            with patch.object(challenge, 'draw', return_value=elsewhere):
                result = await post('/api/Lives/FinishAndValidate', result_payload(life=0))
            after = await current()
            assert (after['generation'], after['completedCount'], after['musicMasterId']) == (2, 1, elsewhere)
            bookmark_key = IDATA_OBJECT_KEY['MusicBookmark']
            assert [v[1] for v in result[2] if v[0] == bookmark_key] == [[pending, 1], [elsewhere, 7]]
            await post('/api/Lives/FinishAndValidate', result_payload(life=0), valid=False)
            assert (await current())['generation'] == 2
            print('PASS: a lesson on the pending song advances once after settling, even on failure; retirement and other lessons do not')

            # The selected object is a song. Each normal difficulty can complete it,
            # with the normal record/rating pipeline and no special chart rewrite.
            for difficulty in (1, 2, 3, 4, 5):
                before = await current()
                music = before['musicMasterId']
                chart = music * 100 + difficulty
                async with app.acquire_db() as c:
                    times_before = await c.conn.fetchval('SELECT "timesCompleted" FROM live WHERE "userId"=$1 AND "liveMasterId"=$2', uid, chart) or 0
                await start(music, difficulty)
                if difficulty == 1:
                    bad = result_payload()
                    bad.base_score_blocks[0].hash += 1
                    await post('/api/Lives/FinishAndValidate', bad, valid=False)
                    assert (await current())['generation'] == before['generation']
                result = await post('/api/Lives/FinishAndValidate', result_payload(life=0 if difficulty == 2 else 1))
                after = await current()
                assert after['generation'] == before['generation'] + 1
                assert after['completedCount'] == before['completedCount'] + 1
                assert after['musicMasterId'] in pool
                changed = {v[1][0]: v[1][1] for v in result[2] if v[0] == IDATA_OBJECT_KEY['MusicBookmark']}
                if after['musicMasterId'] != music:
                    assert changed[music] & 4 == 0
                assert changed[after['musicMasterId']] & 4
                assert [m for m, f in (await markers()).items() if f & 4] == [after['musicMasterId']]
                async with app.acquire_db() as c:
                    assert await c.conn.fetchval('SELECT "timesCompleted" FROM live WHERE "userId"=$1 AND "liveMasterId"=$2', uid, chart) == times_before + 1
                await post('/api/Lives/FinishAndValidate', result_payload(), valid=False)
                assert (await current())['generation'] == after['generation']
            print('PASS: all five chosen difficulties advance once, including a manual failure; ordinary records persist and invalid/retried finishes do not advance')

            before = await current()
            await start(before['musicMasterId'])
            with patch.object(challenge, 'draw', return_value=before['musicMasterId']):
                await post('/api/Lives/FinishAndValidate', result_payload())
            after = await current()
            assert after['musicMasterId'] == before['musicMasterId']
            assert after['generation'] == before['generation'] + 1
            assert [m for m, f in (await markers()).items() if f & 4] == [after['musicMasterId']]
            print('PASS: an independent draw may repeat the same song and still completes exactly one round')

            before = await current()
            music = before['musicMasterId']
            await start(music)
            with patch.object(challenge, '_bookmark', side_effect=RuntimeError('injected challenge marker failure')):
                await post('/api/Lives/FinishAndValidate', result_payload(), status=500)
            assert (await current()) == before, 'A failed marker update committed a partial queue advance'
            async with app.acquire_db() as c:
                assert await c.conn.fetchval('SELECT 1 FROM active_live WHERE "userId"=$1', uid)
            await post('/api/Lives/FinishAndValidate', result_payload())
            assert (await current())['generation'] == before['generation'] + 1
            print('PASS: an injected failure rolls the live and queue back together; retry advances exactly once')

            before = await current()
            await start(before['musicMasterId'])
            results = await asyncio.gather(client.post('/api/Lives/FinishAndValidate', content=pack(result_payload())),
                client.post('/api/Lives/FinishAndValidate', content=pack(result_payload())),
                edit(before['musicMasterId'], 7))
            assert all(r.status_code == 200 for r in results[:2])
            faults = [bool([expand(v) for v in unpack_stream(r.content)][0]) for r in results[:2]]
            assert sorted(faults) == [False, True], 'Concurrent finishes did not settle exactly once'
            after = await current()
            assert after['generation'] == before['generation'] + 1
            assert [m for m, f in (await markers()).items() if f & 4] == [after['musicMasterId']]
            print('PASS: concurrent finish, retry and bookmark editing retain one marker and one queue advance')
            response = await client.post('/api/Lives/Music/EditBookmark',
                content=pack(EditBookmarkPayload(music_master_id=pool[0], bookmark_flag=4)),
                headers={'Authorization': 'Bearer ' + make_session_jwt(other, 'AppStore')})
            assert response.status_code == 200 and envelope(response.content)[1] == [True]
            assert (await markers(other))[pool[0]] == 4

        try:
            await state_action(challenge.enable, other)
        except ValueError as e:
            assert 'already in use' in str(e)
        else:
            raise AssertionError('A nonempty group was overwritten')
        assert (await markers(other))[pool[0]] == 4
        before = await current()
        await state_action(challenge.disable)
        assert not (await current())['enabled'] and not any(f & 4 for f in (await markers()).values())
        await state_action(challenge.enable)
        assert (await current())['musicMasterId'] == before['musicMasterId']
        assert (await current())['generation'] == before['generation']
        print('PASS: account isolation, occupied-group refusal and disable/reenable preserve favorites and the pending song')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    asyncio.run(main(parser.parse_args().database))
