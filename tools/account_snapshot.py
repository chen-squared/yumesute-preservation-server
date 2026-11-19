"""Read a local save while retaining its captured client layout."""
import json

from account_compat import merge_account
from helpers.mastermemory import from_json
from helpers.msgpack import unpack
from helpers.user_data import user_data
from protocol import envelope


async def snapshot(app, uid, *, root, account):
    """Use the same read-only snapshot for the web and command-line exports.

    The database is the source of current progress. Its import baseline lets us
    apply those changes without dropping captured null entries, trailing fields
    or wire value types that the database schema cannot represent.
    """
    current = await user_data(app, uid)
    async with app.acquire_db() as conn:
        exists = await conn.conn.fetchval("SELECT to_regclass('preservation_recovery') IS NOT NULL")
        recovered = (await conn.conn.fetchrow(
            'SELECT snapshot,baseline FROM preservation_recovery WHERE user_id=$1', uid
        )) if exists else None
    if recovered:
        current = merge_account(envelope(recovered['snapshot'])[1],
                                unpack(recovered['baseline']), current)
    if uid == account['user_id'] and account['mode'] == 'import':
        original = envelope((root / 'private/account-snapshot/user-data.response.bin').read_bytes())[1]
        baseline = from_json(json.loads((root / 'private/imported-user-roundtrip.json').read_text()))
        current = merge_account(original, baseline, current)
    return current
