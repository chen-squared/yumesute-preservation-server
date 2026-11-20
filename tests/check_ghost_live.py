"""Ghost Live and settled play history checks on a disposable prepared save copy."""

import argparse
import asyncio
from random import Random
from statistics import median
from unittest.mock import patch

from check_review_regressions import finish_payload


async def main(name):
    if not name.startswith('yumesute_review_checks_') or not name.isidentifier():
        raise SystemExit('Only a disposable yumesute_review_checks_<suffix> database is allowed')
    from helpers.config import config, database
    config['database']['database'] = database.database = name
    import httpx
    import ghost_live
    import ghost_rating
    import ghost_schedule
    import play_history
    import serve
    from core import YumeApp
    from gameplay import State
    from helpers.auth import make_session_jwt
    from helpers.cache import cache
    from helpers.mastermemory import unpack
    from helpers.msgpack import from_array, pack, to_wire
    from models import FinishAnotherNotationLivePayload, FinishLivePayload, StartLessonPayload, StartLivePayload
    from models.enums import MatchingResult, TimingTypes
    from protocol import expand, unpack_stream

    app = serve.app
    async with app.router.lifespan_context(app):
        async with app.acquire_db() as c:
            assert await c.conn.fetchval('SELECT current_database()') == name
            uid = await c.conn.fetchval('SELECT "userId" FROM "character" GROUP BY "userId" ORDER BY count(*) DESC LIMIT 1')
            party = await c.conn.fetchval('SELECT "partyId" FROM party_slot WHERE "userId"=$1 GROUP BY "partyId" '
                                          'HAVING count(*) >= 5 LIMIT 1', uid)
            lesson_base = await c.conn.fetchval('SELECT "characterBaseMasterId" FROM character_base '
                                                'WHERE "userId"=$1 ORDER BY "characterBaseMasterId" LIMIT 1', uid)
            assert party is not None and lesson_base is not None, 'Prepare a save with a party and lesson actors'
            await c.conn.execute(f'DELETE FROM {ghost_live.TABLE} WHERE "userId"=$1', uid)
            await c.conn.execute(f'DELETE FROM {play_history.TABLE} WHERE "userId"=$1', uid)
            choices = await ghost_live.eligible(State(c, uid))

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        auth = {'Authorization': 'Bearer ' + make_session_jwt(uid, 'AppStore')}
        async with httpx.AsyncClient(transport=transport, base_url='http://localhost', headers=auth) as client:
            async def post(action, payload=None, model=None, valid=True, status=200):
                r = await client.post('/api/Lives/' + action, content=pack(payload) if payload is not None else b'')
                assert r.status_code == status, (action, r.status_code)
                if status != 200:
                    return None
                parts = [expand(v) for v in unpack_stream(r.content)]
                if valid is not None:
                    assert bool(parts[0]) != valid, (action, parts[0])
                if parts[0]:
                    return None
                return from_array(model, parts[1]) if model else parts[1]

            manifest = await client.get('/api/data/master')
            assert manifest.status_code == 200
            version = from_array('MasterDataManifest', expand(list(unpack_stream(manifest.content))[1])).uri
            body = await client.get('/master-data/production/' + version)
            assert body.status_code == 200
            archive = unpack(body.content)
            wire = archive['GhostLiveMaster']
            stages = sorted(cache.ghost_live_master, key=lambda row: row.id_)
            assert wire == [to_wire(row) for row in stages]
            assert len(wire) > 8 and ghost_schedule.playable_songs() <= {row[1] for row in wire}
            senses = {row[0]: row for row in archive['SenseNotationMaster']}
            assert all(senses[row[3]][1] for row in wire)
            for stage in choices.values():
                for difficulty in range(1, 6):
                    assert any(m.music_master_id == stage.music_master_id and int(m.difficulty) == difficulty
                               for m in cache.live_master)
            print(f'PASS: the downloaded master has {len(wire)} matches with valid schedules and all five charts')

            async def pending():
                async with app.acquire_db() as c:
                    return await ghost_live.current(State(c, uid))

            async def history():
                async with app.acquire_db() as c:
                    return await c.conn.fetch(f'SELECT * FROM {play_history.TABLE} WHERE "userId"=$1 ORDER BY id', uid)

            music = next(ident for ident, stage in choices.items() if stage.id_ >= ghost_schedule.ADDED_ID_BASE
                         and any(row.music_master_id == ident and row.note_count for row in cache.live_master))
            stage = choices[music]
            charts = {int(row.difficulty): row for row in cache.live_master if row.music_master_id == music}
            finish = 'FinishAndValidate'

            async def match():
                return await post('MatchingGhostLive', model='MatchingGhostLiveResult')

            async def start(chart, **kwargs):
                valid = kwargs.pop('valid', True)
                return await post('StartGhostLive', StartLivePayload(
                    live_master_id=chart.id_, party_id=party, ghost_live_master_id=stage.id_, **kwargs),
                    model='LiveUnit', valid=valid)

            with patch.object(ghost_live, 'draw', return_value=music) as draw:
                first, second = await asyncio.gather(match(), match())
            assert first == second and first.ghost_live_master_id == stage.id_
            draw.assert_called_once()
            assert set(draw.call_args.args[0]) == set(choices)
            assert len({slot.character_master_id for slot in first.ghost_live_info.party_slot}) == 5
            initial = await pending()
            assert initial['targets'] == {}
            new_pool = YumeApp(config=database)
            await new_pool.yume_setup()
            try:
                async with new_pool.acquire_db() as c:
                    assert await ghost_live.current(State(c, uid)) == initial
            finally:
                await new_pool.close()
            print('PASS: a concurrent match draws once from the full owned pool and survives reconnecting to the save')

            await start(next(chart for chart in cache.live_master if chart.music_master_id != music), valid=False)
            await start(charts[4], is_auto_play=True, valid=False)
            assert await pending() == initial
            for diff, chart in charts.items():
                opened = await start(chart)
                assert opened.actors and opened.time_events, diff
                await post('Retire')
                assert await match() == first
            locked = await pending()
            assert set(locked['targets']) == {str(chart.id_) for chart in charts.values()}
            await start(charts[4])
            assert await pending() == locked
            await post(finish, FinishLivePayload(), valid=False)
            good = finish_payload(TimingTypes.PERFECT_STAR)
            bad = good.model_copy(deep=True)
            bad.base_score_blocks[0].hash += 1
            await post(finish, bad, valid=False)
            with patch.object(play_history, 'record', side_effect=RuntimeError('injected history failure')):
                await post(finish, good, status=500)
            assert await pending() == locked and await history() == []
            print('PASS: five selectable charts, retirement, retry, invalid reports and rollback preserve the round and targets')

            def outcome(result, rival, target, expected, wins):
                ghost = result.ghost_live_result
                assert ghost.matching_result == expected and ghost.ghost_user_score == target
                assert ghost.total_battle_win_count == wins
                assert ghost.ghost_user_profile.user_id == rival.ghost_user_profile.user_id
                assert ghost.ghost_user_profile.user_name.endswith(f'{target / play_history.RATE_SCALE:.4f}%')

            async def plays(chart):
                async with app.acquire_db() as c:
                    return await c.conn.fetchval('SELECT "timesCompleted" FROM live '
                                                 'WHERE "userId"=$1 AND "liveMasterId"=$2', uid, chart.id_) or 0

            before = await plays(charts[4])
            results = await asyncio.gather(*(post(finish, good, model='FinishLiveResult', valid=None) for _ in range(2)))
            assert sum(result is not None for result in results) == 1
            target = locked['targets'][str(charts[4].id_)]
            expected = MatchingResult.Win if target < 1010000 else MatchingResult.Draw
            wins = int(expected == MatchingResult.Win)
            outcome(next(result for result in results if result), first, target, expected, wins)
            after = await pending()
            assert after['ghostMasterId'] is None and after['wins'] == wins
            assert await plays(charts[4]) == before + 1

            for fixed, life, expected in ((1010000, 1, MatchingResult.Draw), (990000, 0, MatchingResult.Lose),
                                          (1010000, 1, MatchingResult.Lose)):
                with patch.object(ghost_live, 'draw', return_value=music):
                    repeated = await match()
                assert repeated.ghost_live_master_id == first.ghost_live_master_id
                with patch.object(ghost_rating, 'target', return_value=fixed):
                    await start(charts[5])
                timing = TimingTypes.PERFECT if fixed == 1010000 and expected == MatchingResult.Lose else TimingTypes.PERFECT_STAR
                result = await post(finish, finish_payload(timing, life=life), model='FinishLiveResult')
                outcome(result, repeated, fixed, expected, wins)
                assert (await pending())['ghostMasterId'] is None and (await pending())['wins'] == wins
            print('PASS: achievement-rate win, draw, loss and life depletion settle once, and the next draw can repeat a song')

            chart = charts[4]
            await post('Start', StartLivePayload(party_id=party, live_master_id=chart.id_, is_auto_play=True))
            await post(finish, good)
            await post('StartLesson', StartLessonPayload(character_base_master_id=lesson_base, live_master_id=chart.id_))
            await post(finish, good)
            alternate = next((row for row in cache.another_notation_master if row.music_master_id == music),
                             cache.another_notation_master[0])
            alt_payload = FinishAnotherNotationLivePayload(another_notation_master_id=alternate.id_, is_auto_play=True,
                                                           base_score_blocks=good.base_score_blocks)
            await post('FinishAnotherNotationLive', alt_payload)
            rows = await history()
            assert [row['mode'] for row in rows] == ['ghost'] * 4 + ['normal', 'lesson', 'alternate']
            assert [row['rivalAchievement'] for row in rows] == [target, 1010000, 990000, 1010000, None, None, None]
            assert all(row.get('starActs') is None for row in rows)
            assert [row['isAutoPlay'] for row in rows[-3:]] == [True, False, True]
            assert rows[-1]['anotherNotationMasterId'] == alternate.id_
            assert not rows[2]['cleared'] and rows[3]['cleared']
            assert rows[0]['judgments'] == [20, 0, 0, 0, 0, 0]
            assert (rows[0]['achievement'], rows[0]['maxCombo'], rows[0]['score']) == (1010000, 20, 2020)

            async with app.acquire_db() as c, c.transaction():
                s = State(c, uid)
                recent = await play_history.recent_clears(s, [chart.id_], 20)
                assert len(recent) == 2  # the hand-played Ghost Live and lesson
                for difficulty, counts in ((4, (900, 100, 0, 0, 0, 0)), (5, (850, 0, 150, 0, 0, 0))):
                    for _ in range(play_history.PER_CHART + 1):
                        await play_history.record(s, charts[difficulty].id_, 'normal',
                                                  play_history.Play(counts, 2000, True, 1000))
                    ref, _, samples = await ghost_rating.reference(s, charts[difficulty])
                    assert len(samples) == play_history.PER_CHART
                    assert ref == play_history.rate_points(counts) / play_history.RATE_SCALE
                rng = Random(513)
                targets = [median([await ghost_rating.target(s, charts[d], rng) for _ in range(20)]) for d in (4, 5)]
                assert targets[0] - targets[1] >= 20000, targets
            rows = await history()
            assert len(rows) == 2 * play_history.PER_CHART + 1
            assert all(row['maxCombo'] == 1000 for row in rows if not row['anotherNotationMasterId'])
            print('PASS: normal, lesson, alternate and battle samples persist, rotate per chart, and calibrate each difficulty separately')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    asyncio.run(main(parser.parse_args().database))
