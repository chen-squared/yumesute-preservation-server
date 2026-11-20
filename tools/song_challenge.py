"""A persistent one-song random challenge in an otherwise empty bookmark group."""
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
import random
import time

from db.user import upsert_music_bookmark
from helpers.cache import cache

TABLE = 'preservation_song_challenge'
CHARTS = Path(__file__).resolve().parents[1] / 'vendor/server-of-dreams/_data/assets/Notations'


async def ensure_tables(conn):
    await conn.conn.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
        "userId" bigint PRIMARY KEY REFERENCES accounts("userId") ON DELETE CASCADE,
        enabled boolean NOT NULL,
        "bookmarkFlag" integer NOT NULL CHECK ("bookmarkFlag" IN (1,2,4)),
        "musicMasterId" integer NOT NULL,
        generation bigint NOT NULL DEFAULT 1,
        "completedCount" bigint NOT NULL DEFAULT 0,
        "updatedAt" bigint NOT NULL)''')


async def current(conn, uid):
    # Some standalone vendor checks create a DB pool without the app lifespan.
    if not await conn.conn.fetchval('SELECT to_regclass($1)', TABLE):
        return None
    row = await conn.conn.fetchrow(f'SELECT * FROM {TABLE} WHERE "userId"=$1', uid)
    return dict(row) if row else None


@lru_cache(maxsize=4)
def _catalog(music_identity, live_identity):
    by_music = {}
    for chart in cache.live_master:
        by_music.setdefault(chart.music_master_id, {})[int(chart.difficulty)] = chart
    return frozenset(m.id_ for m in cache.music_master
                     if not m.is_long_version and not m.invisible
                     and set(by_music.get(m.id_, {})) == set(range(1, 6))
                     and (CHARTS / str(m.id_) / 'music_config.enc').is_file()
                     and all((CHARTS / str(m.id_) / f'{d}.enc').is_file() for d in range(1, 6)))


async def candidates(s):
    available = _catalog(id(cache.music_master), id(cache.live_master))
    return sorted({r['musicMasterId'] for r in await s.rows('Music')
                   if r['isPossession'] and r['musicMasterId'] in available})


def draw(eligible, rng=None):
    """Independently sample the complete song pool, with replacement."""
    rng = rng or random.SystemRandom()
    eligible = sorted(set(eligible))
    if not eligible:
        raise ValueError('No owned songs with complete chart resources')
    return rng.choice(eligible)


async def _bookmark(s, music, flag, include):
    existing = await s.conn.conn.fetchrow(
        'SELECT min("rowId") AS ident, COALESCE(bit_or("musicBookmarkFlag"),0) AS flags '
        'FROM music_bookmark WHERE "userId"=$1 AND "musicMasterId"=$2', s.uid, music)
    flags = (int(existing['flags']) & ~flag) | (flag if include else 0)
    if existing['ident'] is None:
        await s.conn.execute(upsert_music_bookmark(s.uid, dict(musicMasterId=music, musicBookmarkFlag=flags)))
    else:
        await s.conn.conn.execute('UPDATE music_bookmark SET "musicBookmarkFlag"=$3 '
                                 'WHERE "userId"=$1 AND "musicMasterId"=$2', s.uid, music, flags)
        await s.conn.conn.execute('DELETE FROM music_bookmark WHERE "userId"=$1 AND "musicMasterId"=$2 '
                                 'AND "rowId"<>$3', s.uid, music, existing['ident'])
    s.tables.pop('MusicBookmark', None)
    s.dirty[('MusicBookmark', music)] = dict(musicMasterId=music, musicBookmarkFlag=flags)


async def enable(s, flag=4):
    """Reserve an empty group; reenabling keeps the pending song."""
    if flag not in (1, 2, 4):
        raise ValueError('Choose bookmark group 1, 2 or 3')
    row = await current(s.conn, s.uid)
    occupied = await s.conn.conn.fetchval(
        'SELECT count(*) FROM music_bookmark WHERE "userId"=$1 AND ("musicBookmarkFlag"&$2)<>0 '
        'AND ($3::integer IS NULL OR "musicMasterId"<>$3)', s.uid, flag,
        row['musicMasterId'] if row and row['enabled'] and row['bookmarkFlag'] == flag else None)
    if occupied:
        raise ValueError('Bookmark group is already in use; existing bookmarks were not changed')
    if row:
        if row['musicMasterId'] not in await candidates(s):
            raise ValueError('The pending song is no longer playable; restore its ownership/resources first')
        if row['enabled'] and row['bookmarkFlag'] != flag:
            await _bookmark(s, row['musicMasterId'], row['bookmarkFlag'], False)
        await s.conn.conn.execute(f'UPDATE {TABLE} SET enabled=true,"bookmarkFlag"=$2,"updatedAt"=$3 '
                                 'WHERE "userId"=$1', s.uid, flag, time.time_ns() // 1000)
    else:
        music = draw(await candidates(s))
        await s.conn.conn.execute(f'INSERT INTO {TABLE} '
            '("userId",enabled,"bookmarkFlag","musicMasterId","updatedAt") '
            'VALUES ($1,true,$2,$3,$4)', s.uid, flag, music, time.time_ns() // 1000)
        row = dict(musicMasterId=music)
    await _bookmark(s, row['musicMasterId'], flag, True)
    return await current(s.conn, s.uid)


async def disable(s):
    row = await current(s.conn, s.uid)
    if row and row['enabled']:
        await _bookmark(s, row['musicMasterId'], row['bookmarkFlag'], False)
        await s.conn.conn.execute(f'UPDATE {TABLE} SET enabled=false,"updatedAt"=$2 WHERE "userId"=$1',
                                 s.uid, time.time_ns() // 1000)


async def preserve_flag(conn, uid, music, requested):
    row = await current(conn, uid)
    if not row or not row['enabled']:
        return requested
    flag = row['bookmarkFlag']
    return (requested & ~flag) | (flag if music == row['musicMasterId'] else 0)


async def start_extra(s, music, auto):
    if auto:
        return {}
    row = await current(s.conn, s.uid)
    if row and row['enabled'] and row['musicMasterId'] == music:
        return {'song_challenge_generation': row['generation']}
    return {}


async def finish(s, context, active, payload):
    token = (context['extra'] or {}).get('song_challenge_generation')
    if (context['mode'] != 'normal' or not token or active['isAutoPlay']
            or not payload.base_score_blocks):
        return
    row = await current(s.conn, s.uid)
    if not row or not row['enabled'] or row['generation'] != token:
        return
    chart = next((m for m in cache.live_master if m.id_ == active['liveMasterId']), None)
    if not chart or chart.music_master_id != row['musicMasterId']:
        return
    music = draw(await candidates(s))
    await s.conn.conn.execute(f'UPDATE {TABLE} SET "musicMasterId"=$2,'
                             'generation=generation+1,"completedCount"="completedCount"+1,"updatedAt"=$3 '
                             'WHERE "userId"=$1', s.uid, music, time.time_ns() // 1000)
    if music != row['musicMasterId']:
        await _bookmark(s, row['musicMasterId'], row['bookmarkFlag'], False)
    await _bookmark(s, music, row['bookmarkFlag'], True)


def install(app):
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with previous(app):
            async with app.acquire_db() as conn:
                await ensure_tables(conn)
            yield
    app.router.lifespan_context = lifespan
