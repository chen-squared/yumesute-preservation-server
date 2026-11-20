"""Enable/status/disable the one-song challenge in an empty native bookmark group."""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import bootstrap
bootstrap()


async def run(args):
    from core import YumeApp
    from gameplay import State
    from helpers.cache import cache, load_master_data
    from helpers.config import database
    import song_challenge

    load_master_data()
    app = YumeApp(config=database)
    await app.yume_setup()
    try:
        async with app.acquire_db() as conn, conn.transaction():
            await conn.conn.execute('SELECT pg_advisory_xact_lock($1::bigint)', args.user_id)
            if not await conn.conn.fetchval('SELECT 1 FROM accounts WHERE "userId"=$1', args.user_id):
                raise ValueError('Account does not exist')
            if args.action == 'enable':
                await song_challenge.ensure_tables(conn)
            state = State(conn, args.user_id)
            if args.action == 'enable':
                await song_challenge.enable(state, 1 << (args.group - 1))
            elif args.action == 'disable':
                await song_challenge.disable(state)
            row = await song_challenge.current(conn, args.user_id)
            if not row:
                print('Challenge has not been enabled for this account.')
                return
            title = next(m.name for m in cache.music_master if m.id_ == row['musicMasterId'])
            print('Enabled:', row['enabled'])
            print('Bookmark group:', row['bookmarkFlag'].bit_length())
            print('Pending song:', title)
            print('Completed:', row['completedCount'])
            print('Songs in random pool:', len(await song_challenge.candidates(state)))
    finally:
        await app.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('enable', 'status', 'disable'))
    parser.add_argument('--user-id', type=int, required=True)
    parser.add_argument('--group', type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except ValueError as exc:
        parser.exit(2, str(exc) + '\n')
