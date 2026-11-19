"""Regressions found while reviewing the local changes against origin/main."""
import copy
import datetime
import json
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools'), str(ROOT / 'vendor/server-of-dreams')]

from helpers.cache import load_master_data
from helpers.msgpack import to_wire
from models import (LiveTimeEvent, LiveUnit, SenseTimingEvent, StartTripleCastLivePayload,
                    TimingEvent)
from models.keys import KEYS
from protocol import envelope
from starlette.requests import Request

load_master_data()

import gameplay
import leagues
import music_courses
import progression


class MemoryState:
    def __init__(self, **tables):
        self.data = copy.deepcopy(tables)
        self.uid = 7
        self.conn = SimpleNamespace(conn=AsyncMock(), execute=AsyncMock())
        self.tables, self.dirty = {}, {}
        self.grants = []

    async def rows(self, name):
        return self.data.setdefault(name, [])

    async def one(self, name, **key):
        return next((r for r in await self.rows(name)
                     if all(r.get(k) == v for k, v in key.items())), None)

    async def update(self, name, row, **values):
        row.update(values)
        self.dirty[(name, row.get('id', 0))] = row.copy()

    async def insert(self, name, **values):
        row = {'id': len(await self.rows(name)) + 1, **values}
        self.data[name].append(row)
        return row

    async def grant(self, things):
        self.grants.extend(things)
        return []

    def present(self):
        return []


class RankCaps(unittest.IsolatedAsyncioTestCase):
    async def test_imported_limit_proves_earlier_gates_were_released(self):
        s = MemoryState(User=[{'id': 1, 'playerRankLimit': 269}])
        with patch.object(gameplay, 'audition_cap_grants', AsyncMock(return_value=220)), \
             patch.object(gameplay, 'released_gate_ranks', AsyncMock(return_value=[])):
            self.assertEqual(await gameplay.grow_rank_cap(s), 1)
        self.assertEqual((await s.one('User'))['playerRankLimit'], 270)

    async def test_reaching_a_gate_raises_the_release_prompt(self):
        m = gameplay.master('player_rank_master', 159, field='rank')
        s = MemoryState(User=[dict(id=1, playerRank=159, playerRankLimit=160,
                                  currentRankPoint=m.point_to_level_up,
                                  currentStamina=0, maxStaminaRestoredAt=0, isCapedPlayerRank=False,
                                  requireCapedPlayerRankAnnounce=False)])
        with patch.object(progression, 'mission_progress', AsyncMock()), \
             patch.object(gameplay, 'released_gate_ranks', AsyncMock(return_value=[])):
            await progression.rank_xp(s, 0)
        user = await s.one('User')
        self.assertEqual(user['playerRank'], 160)
        self.assertTrue(user['isCapedPlayerRank'])
        self.assertTrue(user['requireCapedPlayerRankAnnounce'])

    async def test_rank_up_can_overfill_without_passive_recovery_and_caps_at_3000(self):
        from helpers.stamina import effective_stamina, max_stamina
        m = gameplay.master('player_rank_master', 100, field='rank')
        for held in (1200, 2900, 3000):
            s = MemoryState(User=[dict(id=1, playerRank=100, playerRankLimit=150,
                currentRankPoint=m.point_to_level_up, currentStamina=held,
                maxStaminaRestoredAt=0, isCapedPlayerRank=False,
                requireCapedPlayerRankAnnounce=False)])
            with patch.object(progression, 'mission_progress', AsyncMock()):
                await progression.rank_xp(s, 0)
            user = await s.one('User')
            self.assertEqual(user['currentStamina'], min(3000, held + max_stamina(101)))
            self.assertEqual(effective_stamina(user['currentStamina'], user['maxStaminaRestoredAt'],
                                              max_stamina(101), user['maxStaminaRestoredAt'] + 86400_000000),
                             user['currentStamina'])

    async def test_free_audition_finish_releases_banked_rank_experience(self):
        from models import FinishLivePayload
        m = gameplay.master('player_rank_master', 100, field='rank')
        s = MemoryState(User=[dict(id=1, playerRank=100, playerRankLimit=101,
            currentRankPoint=m.point_to_level_up, currentStamina=1200,
            maxStaminaRestoredAt=0, isCapedPlayerRank=True,
            requireCapedPlayerRankAnnounce=False)])
        with patch.object(progression, 'mission_progress', AsyncMock()):
            await progression.finish_context(s, dict(mode='audition', masterId=1, extra={'rank_xp': 0}),
                                             FinishLivePayload(), SimpleNamespace())
        self.assertEqual((await s.one('User'))['playerRank'], 101)


class CourseRecords(unittest.IsolatedAsyncioTestCase):
    async def test_worse_attempt_keeps_the_best_attempts_note_counts(self):
        s = MemoryState(MusicCourseRanking=[dict(
            id=1, musicCourseMasterId=372, currentChallengeCount=1,
            totalAchievementRatePercentRecord='303.0000', hasReceivedReward=True,
            bestRecordChallengeCount=1, bestRecordDate=123,
            perfectStar=180, perfect=0, great=0, good=0, bad=0, miss=0)])
        run = {'rates': [80.0] * 3, 'notes': [{'great': 60}] * 3}
        with patch.object(music_courses, 'bands', return_value=[object()]), \
             patch.object(music_courses, 'rank_of', AsyncMock(return_value=(1, 100))):
            await music_courses.post_attempt(s, SimpleNamespace(id_=372), run, 2)
        row = await s.one('MusicCourseRanking')
        self.assertEqual(row['currentChallengeCount'], 2)
        self.assertEqual(row['perfectStar'], 180)
        self.assertEqual(row['great'], 0)
        self.assertEqual(row['bestRecordChallengeCount'], 1)

    async def test_rewards_wait_for_close_and_use_the_final_best_record_once(self):
        course = SimpleNamespace(id_=372, start_date='2026-11-01T00:00:00Z',
                                 end_date='2026-11-08T00:00:00Z')
        end = datetime.datetime.fromisoformat(course.end_date).timestamp()
        s = MemoryState(MusicCourse=[dict(musicCourseMasterId=372, certificationGrade=2)])
        band = SimpleNamespace(rewards=[SimpleNamespace(thing_type=6, thing_id=123, thing_quantity=1)])
        with patch.object(music_courses.cache, 'music_course_master', [course]), \
             patch.object(music_courses, 'bands', return_value=[band]), \
             patch.object(music_courses, 'band_for', return_value=band), \
             patch.object(music_courses, 'rank_of', AsyncMock(return_value=(1, 100))) as rank:
            await music_courses.post_attempt(s, course, {'rates': [100.0] * 3}, 2)
            await music_courses.post_attempt(s, course, {'rates': [101.0] * 3}, 2)
            await music_courses.pay_closed(s, now=end - 1)
            self.assertFalse(s.grants)
            self.assertFalse((await s.one('MusicCourseRanking'))['hasReceivedReward'])
            await music_courses.pay_closed(s, now=end)
            rank.assert_awaited_with(s, 372, 303.0)
            self.assertEqual(s.grants, [(6, 123, 1)])
            await music_courses.pay_closed(s, now=end + 1)
            self.assertEqual(s.grants, [(6, 123, 1)])


class TripleCastWire(unittest.IsolatedAsyncioTestCase):
    async def test_time_event_map_contains_timing_events(self):
        s = MemoryState(TripleCastParty=[dict(id=i, order=i) for i in (1, 2, 3)])
        window = SimpleNamespace(music=27, sense_ids=(1, 2, 3), id=3041, difficulty=5)
        chart = SimpleNamespace(music_master_id=27, difficulty=3)
        timing = TimingEvent(is_star_act=True)
        notation = LiveTimeEvent(timings=[SenseTimingEvent(
            timing_seconds=12, position=1, event=timing)])

        @asynccontextmanager
        async def transaction(_):
            yield s

        request = Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': []})
        with patch.object(leagues, 'read_request', AsyncMock(return_value=StartTripleCastLivePayload(
                m_live_id=2703, m_triple_cast_id=3041))), \
             patch.object(leagues, 'window_for', return_value=window), \
             patch.object(leagues, 'master', return_value=chart), \
             patch.object(leagues, 'transaction', transaction), \
             patch.object(leagues, 'begin', AsyncMock()), \
             patch.object(leagues, 'basic', AsyncMock(return_value=dict(
                 partyOrder1=1, partyOrder2=2, partyOrder3=3))), \
             patch('helpers.live.build_live_unit', AsyncMock(return_value=(LiveUnit(), 77))), \
             patch('helpers.live.build_live_time_event', AsyncMock(return_value=notation)):
            response = await leagues.start_triple_cast_live(request)
        units = envelope(response.body)[1][0]
        index = next(k for k, field, *_ in KEYS['LiveUnitWithOrder'] if field == 'time_events')
        self.assertEqual(len(units), 3)
        for unit in units:
            self.assertEqual(unit[index][12], to_wire(timing))


class LeagueBorders(unittest.IsolatedAsyncioTestCase):
    async def test_keep_cut_includes_promoted_players_and_empty_keep_band_stays_hidden(self):
        s = MemoryState()
        rivals = [{'classType': 3, 'score': 1000 - i} for i in range(99)]
        for keep, expected in ((30, 90), (0, None)):
            cls = SimpleNamespace(up_user_amount=60, keep_user_amount=keep)
            with patch.object(leagues, 'basic', AsyncMock(return_value={'currentClassType': 4})), \
                 patch.object(leagues, 'group', AsyncMock(return_value={'classType': 3})), \
                 patch.object(leagues, 'member', AsyncMock(return_value={'bestScore': 500})), \
                 patch.object(leagues, 'rivals', AsyncMock(return_value=rivals)), \
                 patch.object(leagues, 'ladder', return_value=cls):
                result = await leagues.standings(s, 'league', SimpleNamespace())
            self.assertEqual(result['class_type'], 3, 'A later promotion changed an earlier group')
            self.assertEqual(result['borders'].get('keep_border_ranking'), expected)


class PublishedSchedules(unittest.TestCase):
    def test_both_ladders_high_difficulties_have_archived_rows_and_chart_files(self):
        import league_schedule
        from helpers.cache import cache
        from live_finish import _olivier_sp_rate_points
        from helpers.live_rate import chart_live_rate
        charts = {(c.music_master_id, int(c.difficulty)) for c in cache.live_master}
        for kind in ('league', 'triple_cast'):
            self.assertTrue(all(w.difficulty == 5 for w in league_schedule.generated(kind)))
            for music in league_schedule.pool(kind):
                for difficulty in (4, 5):
                    self.assertIn((music, difficulty), charts)
                    self.assertTrue((ROOT / 'vendor/server-of-dreams/_data/assets/Notations'
                                     / str(music) / f'{difficulty}.enc').is_file())
                    chart = next(c for c in cache.live_master
                                 if c.music_master_id == music and int(c.difficulty) == difficulty)
                    self.assertIsNone(chart_live_rate(chart.id_, 101.0))
                    self.assertIsNone(_olivier_sp_rate_points(chart.id_, 101.0, 6, 0))

    def test_archived_league_windows_keep_their_original_end(self):
        import league_schedule
        import preservation_dates as dates
        from helpers.mastermemory import unpack
        tables = unpack(dates.preserved_master()[0])
        for kind, table in (('league', 'LeagueMaster'), ('triple_cast', 'TripleCastMaster')):
            end = next(k for k, field, *_ in KEYS[table] if field == 'display_end_at')
            last = league_schedule.anchor(kind)
            row = next(r for r in tables[table] if r[0] == last.id)
            self.assertEqual(row[end].seconds, last.display_end)

    def test_future_weekly_ids_do_not_replace_anniversary_courses(self):
        import course_schedule
        official = json.loads((course_schedule.MASTERDATA / 'MusicCourseMaster.json').read_text())
        occupied = {r['id'] for r in official}
        horizon = int(datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc).timestamp())
        generated = course_schedule.generated(now=horizon)
        self.assertFalse(occupied & {r.id for r in generated})
        self.assertEqual(len({r.id for r in generated}), len(generated))

    def test_a_running_server_publishes_new_windows_past_its_initial_horizon(self):
        import course_schedule
        import league_schedule
        import preservation_dates as dates
        now = league_schedule.anchor('league').display_end + 30 * 86400
        dates.preserved.cache_clear()
        try:
            with patch('time.time', return_value=now), \
                 patch('time.time_ns', return_value=now * 1_000_000_000):
                old = dates.preserved_master()
            with patch('time.time', return_value=now + 61 * 86400), \
                 patch('time.time_ns', return_value=(now + 61 * 86400) * 1_000_000_000):
                new = dates.preserved_master()
                current_id = course_schedule.live()[0].id
                ids = {ident for table, ident, _ in new[2] if table == 'MusicCourseMaster'}
                self.assertIn(current_id, ids)
            self.assertNotEqual(old[1], new[1])
        finally:
            dates.preserved.cache_clear()


if __name__ == '__main__':
    unittest.main()
