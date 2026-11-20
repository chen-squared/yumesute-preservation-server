"""Local Ghost Live matching. The song and the opponent persist until settlement."""

from contextlib import asynccontextmanager
from random import Random
from secrets import randbits

from fastapi import APIRouter, Request
from gameplay import Rejected, master, transaction
from helpers.cache import cache
from helpers.msgpack import fault, respond
from models import GhostLiveInfo, GhostLiveResult, MatchingGhostLiveResult, PartySlotDetail, UserProfileDetail
from models.enums import MatchingResult

import ghost_rating
import ghost_schedule
from play_history import RATE_SCALE
from song_challenge import draw


TABLE = 'preservation_ghost_match'
LABEL = 'ローカル対抗'
router = APIRouter()


async def ensure_table(conn):
    await conn.conn.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
        "userId" bigint PRIMARY KEY REFERENCES accounts("userId") ON DELETE CASCADE,
        "ghostMasterId" bigint,
        seed bigint NOT NULL,
        targets jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        wins bigint NOT NULL DEFAULT 0)''')


async def current(s):
    return await s.conn.conn.fetchrow(f'SELECT * FROM {TABLE} WHERE "userId"=$1', s.uid)


def opponent(seed, points=None):
    rng = Random(seed)
    # Starter actor cards are already used by the client itself, including the home icon
    # and the party inspection panel. Use real, distinct card IDs for those lookups.
    pool = sorted((card for card in cache.character_master if int(card.rarity) == 1), key=lambda card: card.id_)
    cards = rng.sample(pool, 5)
    level = rng.randint(15, 25)
    slots = [PartySlotDetail(position=i, character_master_id=card.id_, character_level=level,
                             current_status_vocal=800, current_status_expression=800,
                             current_status_concentration=800)
             for i, card in enumerate(cards, start=1)]
    name = f'{LABEL} {seed % 100:02d}' if points is None else f'{LABEL} {points / RATE_SCALE:.4f}%'
    profile = UserProfileDetail(user_id=f'local-ghost-{seed}', user_name=name,
                                main_m_character_id=cards[0].id_, main_character_level=level)
    return profile, GhostLiveInfo(leader_position=1, party_slot=slots)


async def eligible(s):
    owned = {row['musicMasterId'] for row in await s.rows('Music') if row['isPossession']}
    playable = ghost_schedule.playable_songs()
    by_song = {}
    for row in sorted(cache.ghost_live_master, key=lambda row: row.id_):
        if row.music_master_id in owned and row.music_master_id in playable:
            by_song.setdefault(row.music_master_id, row)
    return by_song


@router.post('/api/Lives/MatchingGhostLive')
async def match(request: Request):
    try:
        async with transaction(request) as s:
            pending = await current(s)
            choices = await eligible(s)
            if not choices:
                raise Rejected('No playable short songs for Ghost Live')
            held = master('ghost_live_master', pending['ghostMasterId']) if pending else None
            if held is None or held.music_master_id not in choices:
                held = choices[draw(choices)]
                seed = randbits(63)
                await s.conn.conn.execute(f'''INSERT INTO {TABLE} ("userId","ghostMasterId",seed)
                    VALUES ($1,$2,$3) ON CONFLICT ("userId") DO UPDATE
                    SET "ghostMasterId"=EXCLUDED."ghostMasterId",seed=EXCLUDED.seed,targets='{{}}'::jsonb''',
                    s.uid, held.id_, seed)
            else:
                seed = pending['seed']
            profile, info = opponent(seed)
        return respond(MatchingGhostLiveResult(ghost_live_master_id=held.id_, ghost_user_profile=profile,
                                               ghost_live_info=info))
    except Rejected as exc:
        return respond(MatchingGhostLiveResult(), faults=[fault('InvalidLiveState', str(exc))])


async def begin(s, payload, chart):
    pending = await current(s)
    if not pending or not pending['ghostMasterId'] or pending['ghostMasterId'] != payload.ghost_live_master_id:
        raise Rejected('Match a local opponent first')
    stage = master('ghost_live_master', pending['ghostMasterId'])
    if (not stage or stage.music_master_id != chart.music_master_id or payload.is_auto_play
            or stage.music_master_id not in await eligible(s)):
        raise Rejected('Choose a chart of the matched song for manual play')
    key = str(chart.id_)
    targets = dict(pending['targets'])
    if key not in targets:
        rng = Random(f"{pending['seed']}:{chart.id_}")
        targets[key] = await ghost_rating.target(s, chart, rng)
        await s.conn.conn.execute(f'UPDATE {TABLE} SET targets=$2 WHERE "userId"=$1', s.uid, targets)
    return stage, {'ghost_seed': pending['seed']}


async def finish(s, context, active, play, result):
    pending = await current(s)
    if (play is None or active['isAutoPlay'] or not pending or pending['ghostMasterId'] != context['masterId']
            or pending['seed'] != context['extra'].get('ghost_seed')):
        raise Rejected('Missing Ghost Live result or match')
    key = str(active['liveMasterId'])
    if key not in pending['targets']:
        raise Rejected('Missing rival target for this chart')
    target = pending['targets'][key]
    if not play.cleared or play.points < target:
        outcome = MatchingResult.Lose
    elif play.points == target:
        outcome = MatchingResult.Draw
    else:
        outcome = MatchingResult.Win
    wins = pending['wins'] + int(outcome == MatchingResult.Win)
    profile, _ = opponent(pending['seed'], target)
    result.ghost_live_result = GhostLiveResult(ghost_user_profile=profile, ghost_user_score=target,
        total_battle_win_count=wins, matching_result=outcome, display_character_master_id=profile.main_m_character_id)
    await s.conn.conn.execute(f'''UPDATE {TABLE} SET "ghostMasterId"=NULL,targets='{{}}'::jsonb,wins=$2
        WHERE "userId"=$1''', s.uid, wins)


def install(app):
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with previous(app):
            ghost_schedule.extend_cache()
            async with app.acquire_db() as conn:
                await ensure_table(conn)
            yield

    app.router.lifespan_context = lifespan
    count = len(app.router.routes)
    app.include_router(router)
    app.router.routes[:] = app.router.routes[count:] + app.router.routes[:count]
