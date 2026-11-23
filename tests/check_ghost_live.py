"""Ghost Live and settled play history checks on a disposable prepared save copy."""

import argparse
import asyncio
from datetime import datetime
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
    from league_schedule import JST
    from models import (FinishAnotherNotationLivePayload, FinishLivePayload, SenseScoreBlock,
                        StartLessonPayload, StartLivePayload)
    from models.enums import MatchingResult, TimingTypes
    from protocol import expand, unpack_stream

    assert ghost_live.rival_display_score(2_400_000, 999_375, 999_231) == 2_399_654
    assert ghost_live.rival_display_score(2_020, 1_010_000, 1_009_999) == 2_019
    assert ghost_live.rival_display_score(2_020, 1_000_000, 1_000_001) == 2_021
    assert ghost_live.rival_display_score(0, 0, 0) == 0
    assert ghost_live.rival_display_score(0, 0, 990_000) == 1

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
            with patch.object(ghost_live, 'PAST_SELF_CHANCE', 1.0):
                for diff, chart in charts.items():
                    opened = await start(chart)
                    assert opened.actors and opened.time_events, diff
                    await post('Retire')
                    assert await match() == first
            locked = await pending()
            assert set(locked['targets']) == {str(chart.id_) for chart in charts.values()}
            assert all(isinstance(target, int) for target in locked['targets'].values())
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
                assert ghost.matching_result == expected
                assert ghost.total_battle_win_count == wins
                assert ghost.ghost_user_profile.user_id == rival.ghost_user_profile.user_id
                assert ghost.ghost_user_profile.user_name.endswith(f'{target / play_history.RATE_SCALE:.4f}%')
                return ghost.ghost_user_score

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
            display_score = outcome(next(result for result in results if result), first, target, expected, wins)
            assert abs(display_score - 2020 * target / 1010000) <= 1
            assert (display_score < 2020) == (expected == MatchingResult.Win)
            after = await pending()
            assert after['ghostMasterId'] is None and after['wins'] == wins
            assert await plays(charts[4]) == before + 1

            strong_party = finish_payload(TimingTypes.PERFECT)
            strong_party.sense_score_blocks = [SenseScoreBlock(score=2_397_980, hash=2_397_980)]
            for fixed, payload, expected, displayed in (
                (1010000, good, MatchingResult.Draw, 2020),
                (990000, finish_payload(TimingTypes.PERFECT_STAR, life=0), MatchingResult.Lose, 1980),
                (1010000, strong_party, MatchingResult.Lose, 2_424_000),
            ):
                with patch.object(ghost_live, 'draw', return_value=music):
                    repeated = await match()
                assert repeated.ghost_live_master_id == first.ghost_live_master_id
                with patch.object(ghost_live, 'PAST_SELF_CHANCE', 0.0), patch.object(ghost_rating, 'target', return_value=fixed):
                    await start(charts[5])
                result = await post(finish, payload, model='FinishLiveResult')
                assert outcome(result, repeated, fixed, expected, wins) == displayed
                assert (await pending())['ghostMasterId'] is None and (await pending())['wins'] == wins
            print('PASS: wins use achievement rate, while rival display scores scale to the full player score')

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

            past_chart = charts[2]
            await post('Start', StartLivePayload(party_id=party, live_master_id=past_chart.id_))
            await post(finish, finish_payload(TimingTypes.PERFECT))
            past = (await history())[-1]
            assert past['achievement'] == 1_000_000 and past['liveMasterId'] == past_chart.id_
            async with app.acquire_db() as c:
                s = State(c, uid)
                await play_history.record(s, past_chart.id_, 'normal', play_history.summarize(good), auto=True)
                failed = play_history.summarize(finish_payload(TimingTypes.GREAT, life=0))
                await play_history.record(s, past_chart.id_, 'normal', failed)

            with patch.object(ghost_live, 'draw', return_value=music):
                secret_match = await match()
            assert secret_match.ghost_user_profile.user_name.startswith(ghost_live.LABEL)
            with patch.object(ghost_live, 'PAST_SELF_CHANCE', 1.0), patch.object(ghost_rating, 'target') as simulated:
                await start(past_chart)
            simulated.assert_not_called()
            saved_target = {'achievement': past['achievement'], 'finishedAt': past['finishedAt']}
            assert (await pending())['targets'][str(past_chart.id_)] == saved_target
            await post('Retire')
            assert await match() == secret_match
            with patch.object(ghost_live, 'PAST_SELF_CHANCE', 0.0), patch.object(ghost_rating, 'target') as simulated:
                await start(past_chart)
            simulated.assert_not_called()
            result = await post(finish, good, model='FinishLiveResult')
            score = outcome(result, secret_match, past['achievement'], MatchingResult.Win, wins + 1)
            assert score == 2000
            day = datetime.fromtimestamp(past['finishedAt'] / 1_000_000, JST).strftime('%m/%d')
            assert result.ghost_live_result.ghost_user_profile.user_name == f'過去の自分 {day} 100.0000%'
            assert (await history())[-1]['rivalAchievement'] == past['achievement']
            print('PASS: a same-chart manual clear can be locked as a hidden rival and revealed only at settlement')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    asyncio.run(main(parser.parse_args().database))
