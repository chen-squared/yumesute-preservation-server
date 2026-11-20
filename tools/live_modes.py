"""Master-driven Anthology and audition progression, serialized with live results."""
from contextlib import asynccontextmanager
from contextvars import ContextVar
import time

from fastapi import APIRouter, Request
from gameplay import State, Rejected, master, transaction
from helpers.cache import cache
from helpers.msgpack import respond, read_request, from_array, fault
from helpers.live import build_live_time_event
from helpers.live_result import play_totals
from helpers.score import verify_score_blocks
from helpers.stamina import effective_stamina, max_stamina
from live_parity import reset_auto_play, set_auto_play
from models import StartLivePayload, FinishLivePayload, FinishLiveResult, ConcertResult
from routes import lives as upstream
from scripts._sirius import _unpack_all, _decompress
import play_history

router = APIRouter()
_start_cost = ContextVar('live_start_stamina_cost', default=None)
_vendor_stamina_cost = upstream._stamina_cost


def _stamina_cost(live_master_id, ratio):
    cost = _start_cost.get()
    return _vendor_stamina_cost(live_master_id, ratio) if cost is None else cost


async def affordable_stamina_cost(s, music, ratio):
    base = music.stamina_consumption
    if music.is_long_version or base <= 0:
        return 0
    # Keep the balance stable until the vendor start route makes its atomic debit.
    row = await s.conn.conn.fetchrow('SELECT "currentStamina","maxStaminaRestoredAt","playerRank" '
                                    'FROM "user" WHERE "userId"=$1 FOR UPDATE', s.uid)
    if row is None:
        return 0
    stamina = effective_stamina(row['currentStamina'], row['maxStaminaRestoredAt'],
                                max_stamina(row['playerRank']), time.time_ns() // 1000)
    return base * min(max(1, ratio), stamina // base)


class BoundApp:
    def __init__(self, app, conn): self.app, self.conn = app, conn
    def __getattr__(self, name): return getattr(self.app, name)
    @asynccontextmanager
    async def acquire_db(self): yield self.conn

async def call_bound(request, s, fn, *args, **kwargs):
    original = request.scope['app']
    request.scope['app'] = BoundApp(original, s.conn)
    try: return await fn(request, *args, **kwargs)
    finally: request.scope['app'] = original

def parts(response):
    return [_decompress(v) for v in _unpack_all(response.body)]

def concert_available(stage, cleared):
    siblings = sorted((m.id_ for m in cache.concert_stage_master if m.concert_master_id == stage.concert_master_id))
    pos = siblings.index(stage.id_)
    return stage.id_ in cleared or pos == 0 or siblings[pos-1] in cleared

def phases(ident):
    return sorted((p for p in cache.audition_phase_master if p.auditionaster_id == ident), key=lambda p:p.phase)

def attained(ident, score, acts, cleared):
    return max([p.phase for p in phases(ident) if cleared and score >= p.clear_score and acts >= (p.star_act_count or 0)] + [0])

def rewards(rows):
    return [(int(r.thing_type), r.thing_id, r.thing_quantity) for r in rows or []]

@router.post('/api/Lives/Start')
@router.post('/api/Lives/StartConcert')
@router.post('/api/Lives/StartGhostLive')
async def start(request: Request):
    try:
        p = await read_request(request, StartLivePayload)
        if p is None: raise Rejected('Missing payload')
        concert = request.url.path.endswith('StartConcert')
        ghost = request.url.path.endswith('StartGhostLive') or bool(p.ghost_live_master_id)
        window = None
        if ghost:
            if concert or p.concert_stage_master_id or p.league_master_id or p.audition_master_id:
                raise Rejected('Conflicting Ghost Live mode')
            mode, ident, m = 'ghost', p.ghost_live_master_id, None
        elif getattr(p, 'league_master_id', None):
            from leagues import begin, ladder_stamina_cost, window_for
            window, mode, ident = window_for('league', p.league_master_id), 'league', p.league_master_id
            m = None
        else:
            mode, ident = ('concert', p.concert_stage_master_id) if concert else ('audition', p.audition_master_id)
            m = master('concert_stage_master' if concert else 'audition_master', ident) if ident else None
        chart = master('live_master', p.live_master_id)
        if not chart or (concert and not m) or (ident and not (m or window or ghost)) or (
                m and chart.music_master_id != m.music_master_id) or (
                window and (chart.music_master_id != window.music or int(chart.difficulty) > window.difficulty)):
            raise Rejected('Invalid stage or chart')
        music = master('music_master', chart.music_master_id)
        if not music:
            raise Rejected('Missing song')
        async with transaction(request) as s:
            if not await s.one('Party', id=p.party_id): raise Rejected('Missing party')
            if concert and not concert_available(m, {r['concertStageMasterId'] for r in await s.rows('ConcertStage')}):
                raise Rejected('Stage is locked')
            from progression import daily, context, rules
            await daily(s)
            ghost_stage, ghost_extra = None, {}
            if ghost:
                import ghost_live
                ghost_stage, ghost_extra = await ghost_live.begin(s, p, chart)
            cost = 0
            if window:
                await begin(s, 'league', window)
                cost = ladder_stamina_cost(music)
            elif p.use_stamina:
                cost = await affordable_stamina_cost(s, music, p.stamina_consumption_ratio)
            # The vendor reads the original request again. Give its debit the chosen
            # cost while preserving the payload and the rest of its live-start behavior.
            price_token = _start_cost.set(cost)
            token = set_auto_play(p.is_auto_play)
            try:
                r = parts(await call_bound(request, s, upstream.lives_start))
            finally:
                reset_auto_play(token)
                _start_cost.reset(price_token)
            await s.conn.conn.execute('UPDATE active_live SET "isAutoPlay"=$2 WHERE "userId"=$1', s.uid, bool(p.is_auto_play))
            active = await s.conn.conn.fetchrow('SELECT * FROM active_live WHERE "userId"=$1',s.uid)
            spent = cost if active['staminaSpent'] else 0
            xp=int(spent*rules()['rank_xp_per_stamina'])
            if spent:
                # 消費スタミナ: the daily and weekly counters track the stamina this run
                # really paid, which a ladder run never does (leagues.ladder_stamina_cost).
                import missions
                await missions.spend_stamina(s,spent)
            current_mode = mode if (m or window or ghost_stage) else 'normal'
            extra = {'rank_xp':xp,'auto':p.is_auto_play,**ghost_extra}
            if current_mode == 'normal':
                import song_challenge
                extra.update(await song_challenge.start_extra(s, chart.music_master_id, p.is_auto_play))
            await context(s,current_mode,ident or 0,extra)
            await s.conn.conn.execute('DELETE FROM preservation_course_run WHERE "userId"=$1',s.uid)
            # The sense notation the run plays against: the stage's own for concert and
            # audition, the league window's bonus set for a ladder run.
            notation = (m.music_master_id, m.sense_notation_master_id) if m else (
                window.music, window.sense_ids[0]) if window else None
            if ghost_stage:
                notation = (ghost_stage.music_master_id, ghost_stage.sense_notation_master_id)
            if notation:
                unit = from_array('LiveUnit', r[1])
                lte = await build_live_time_event(s.conn, s.uid, p.party_id, *notation)
                unit.time_events = {t.timing_seconds:t.event for t in lte.timings}
                r[1] = unit
        return respond(r[1], faults=r[0], present=r[2]+s.present())
    except Rejected as e:
        return respond(None, faults=[fault('InvalidLiveStage', str(e))])

async def apply_progress(s, mode, ident, score, acts, cleared, result):
    m = master('concert_stage_master' if mode == 'concert' else 'audition_master', ident)
    if not m: raise Rejected('Missing stage master')
    if mode == 'concert':
        received = []
        if cleared and score >= m.clear_score and not await s.one('ConcertStage', concertStageMasterId=ident):
            received = await s.grant(rewards(m.rewards))
            await s.insert('ConcertStage', concertStageMasterId=ident)
        result.concert_result = ConcertResult(rewards=received)
        return
    row = await s.one('AuditionClear', auditionMasterId=ident)
    before = max(row['clearPhase'], row['skipClearPhase']) if row else 0
    achieved = min(before + 1, attained(ident, score, acts, cleared))
    result.audition_master_id = ident
    result.audition_before_phase = before
    result.audition_after_phase = max(before, achieved)
    things = []
    # The official challenge clear awards the stages in their master order.
    targets = sorted((x for x in cache.audition_master if x.audition_group_number == m.audition_group_number and x.id_ <= ident), key=lambda x:x.id_)
    for target in targets:
        target_phases = phases(target.id_)
        new = min(achieved, max([p.phase for p in target_phases]+[0]))
        if not new: continue
        existing = await s.one('AuditionClear', auditionMasterId=target.id_)
        old = max(existing['clearPhase'], existing['skipClearPhase']) if existing else 0
        if new <= old: continue
        for phase in target_phases:
            if old < phase.phase <= new:
                package = master('audition_reward_package_master', phase.audition_reward_package_master_id)
                if not package: raise Rejected('Missing reward package')
                things.extend(rewards(package.rewards))
        field = 'clearPhase' if target.id_ == ident else 'skipClearPhase'
        if existing:
            if new > existing[field]: await s.update('AuditionClear', existing, **{field:new})
        else:
            values = dict(auditionMasterId=target.id_, clearPhase=0, skipClearPhase=0, auditionClearPartyId=0)
            values[field] = new
            await s.insert('AuditionClear', **values)
    # Preserve reward entries in their official order (including repeated item types).
    result.audition_rewards = []
    for thing in sorted(things, key=lambda t:t[0]):
        result.audition_rewards.extend(await s.grant([thing]))

@router.post('/api/Lives/FinishAndValidate')
async def finish(request: Request):
    return await settle(request, await read_request(request, FinishLivePayload))

async def settle(request: Request, p, *, another_notation_master_id=None):
    """The finish body, for a run whose payload has already been read.

    A request body parses under one model only, so a run that arrives on another route -
    a やねうら alternate chart reported by alternate_notations - settles here with the
    payload that route decoded.
    """
    try:
        if p is None or not verify_score_blocks(p): raise Rejected('Invalid score blocks')
        async with transaction(request) as s:
            active = await s.conn.conn.fetchrow('SELECT * FROM active_live WHERE "userId"=$1', s.uid)
            if not active: raise Rejected('No active live')
            context = await s.conn.conn.fetchrow('SELECT * FROM preservation_live_context WHERE "userId"=$1', s.uid)
            alternate = None
            if context and context['mode'] == 'course':
                from progression import find_course
                _, details, index = find_course(context['masterId'])
                alternate = details[index].another_notation_master_id
            if another_notation_master_id is not None and (
                    not alternate or int(alternate) != int(another_notation_master_id)):
                raise Rejected('Alternate chart is not this course stage')
            # A meme course plays different charts from the underlying song's own chart.
            # It advances the course, without granting that song's lamps/rating/drops.
            r = parts(await call_bound(request, s, upstream.lives_finish_and_validate, p,
                                       record_live=not bool(alternate)))
            if r[0]: raise Rejected('Live validation failed')
            result = from_array('FinishLiveResult', r[1])
            play = play_history.summarize(p)
            if context:
                score, cleared = (play.score, play.cleared) if play else play_totals(p)
                if context['mode'] in ('concert','audition'):
                    await apply_progress(s, context['mode'], context['masterId'], score, len(p.star_act_score_blocks or []), cleared, result)
                if context['mode'] in ('league','triple_cast'):
                    # The ladder posts its own result: best score, the party that got it,
                    # the per-play reward package and the weekly league mission.
                    from leagues import score_ladder
                    await score_ladder(s, context, active, p, result)
                if context['mode'] == 'ghost':
                    import ghost_live
                    await ghost_live.finish(s, context, active, play, result)
                from progression import finish_context
                await finish_context(s,context,p,result)
                import song_challenge
                await song_challenge.finish(s, context, active, p)
            await play_history.record(s, active['liveMasterId'], context['mode'] if context else 'normal',
                                      play, auto=active['isAutoPlay'], alternate=alternate or 0,
                                      rival=result.ghost_live_result.ghost_user_score if result.ghost_live_result else None)
            await s.conn.conn.execute('DELETE FROM preservation_live_context WHERE "userId"=$1', s.uid)
            changed = s.present()
            keys = {(v[0],v[1][0]) for v in changed}
            present = [v for v in r[2] if (v[0],v[1][0]) not in keys] + changed
        return respond(result, present=present)
    except Rejected as e:
        return respond(FinishLiveResult(), faults=[fault('InvalidLiveState', str(e))])

@router.post('/api/Lives/Retire')
async def retire(request: Request):
    async with transaction(request) as s:
        await s.conn.conn.execute('DELETE FROM preservation_live_context WHERE "userId"=$1', s.uid)
        await s.conn.conn.execute('DELETE FROM preservation_course_run WHERE "userId"=$1',s.uid)
        return await call_bound(request, s, upstream.lives_retire)

def install(app):
    upstream._stamina_cost = _stamina_cost
    previous = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        async with previous(app):
            from scoring_compat import repair_character_stats
            repair_character_stats()
            from preservation_dates import repair_cached_dates
            repair_cached_dates()
            async with app.acquire_db() as conn:
                await conn.conn.execute('CREATE TABLE IF NOT EXISTS preservation_live_context ("userId" bigint PRIMARY KEY REFERENCES accounts("userId") ON DELETE CASCADE, mode text NOT NULL, "masterId" integer NOT NULL)')
                await conn.conn.execute("ALTER TABLE preservation_live_context ADD COLUMN IF NOT EXISTS extra jsonb NOT NULL DEFAULT '{}'::jsonb")
                await conn.conn.execute('CREATE TABLE IF NOT EXISTS preservation_course_run ("userId" bigint PRIMARY KEY REFERENCES accounts("userId") ON DELETE CASCADE, data jsonb NOT NULL)')
                await conn.conn.execute('ALTER TABLE active_live ADD COLUMN IF NOT EXISTS "isAutoPlay" boolean NOT NULL DEFAULT false')
                await play_history.ensure_table(conn)
                from things_compat import repair_dual_cards
                async with conn.transaction():
                    repaired = await repair_dual_cards(conn)
                if repaired:
                    print(f"Linked the second idol on {repaired} dual-idol card(s)", flush=True)
            yield
    app.router.lifespan_context = lifespan
    count = len(app.router.routes)
    app.include_router(router)
    app.router.routes[:] = app.router.routes[count:] + app.router.routes[:count]
