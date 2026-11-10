"""Export the current local account(s) as verified, re-importable account ZIPs."""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'vendor/server-of-dreams'))
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))

from server import bootstrap

bootstrap()

from helpers.config import database
from helpers.msgpack import pack
from exporter import save_snapshot, verify_export


def run(output, only):
    from core import YumeApp

    async def go():
        app = YumeApp(config=database)
        await app.yume_setup()
        try:
            from helpers.user_data import user_data
            async with app.acquire_db() as conn:
                ids = [r['userId'] for r in await conn.conn.fetch('SELECT "userId" FROM accounts ORDER BY "userId"')]
            for uid in ids:
                if only and uid != only:
                    continue
                result = await user_data(app, uid)
                # Same five-part MessagePack-CSharp envelope the game receives on GET /api/data/user.
                body = b''.join([pack([]), pack(result), pack([]), pack([]), pack([])])
                dest, manifest = save_snapshot(output, body, client_version='2.31.3.425',
                                               source='local-server-account-export')
                checked = verify_export(dest)
                s = checked['summary']
                print('EXPORTED user %s -> %s' % (uid, dest))
                print('   verified: %s records; %s actors, %s posters, %s songs, %s costumes'
                      % (checked['entries'], s['Character'], s['Poster'], s['Music'], s['Costume']))
        finally:
            await app.close()

    asyncio.run(go())


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=ROOT / 'backups/account-exports')
    p.add_argument('--user-id', type=int, help='export only this account')
    a = p.parse_args()
    run(a.output, a.user_id)
