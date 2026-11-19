import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools'), str(ROOT / 'vendor/server-of-dreams')]

import account_snapshot
from helpers.mastermemory import to_json
from helpers.msgpack import pack


class AccountSnapshots(unittest.IsolatedAsyncioTestCase):
    async def snapshot(self, root, account, recovered=None, table_exists=True):
        conn = SimpleNamespace(conn=SimpleNamespace(
            fetchval=AsyncMock(return_value=table_exists),
            fetchrow=AsyncMock(return_value=recovered)))

        @asynccontextmanager
        async def acquire():
            yield conn

        with patch.object(account_snapshot, 'user_data', AsyncMock(return_value=[[0, [7, 11]]])):
            return await account_snapshot.snapshot(SimpleNamespace(acquire_db=acquire), 7,
                                                   root=root, account=account)

    async def test_initial_import_retains_unrepresented_fields_and_current_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'private/account-snapshot').mkdir(parents=True)
            original = [None, [0, [7, 10.0, None, 'captured trailing field']]]
            baseline = [[0, [7, 10]]]
            body = b''.join(pack(part) for part in ([], original, [], [], []))
            (root / 'private/account-snapshot/user-data.response.bin').write_bytes(body)
            (root / 'private/imported-user-roundtrip.json').write_text(json.dumps(to_json(baseline)))
            result = await self.snapshot(root, {'user_id': 7, 'mode': 'import'})
            self.assertEqual(result, [None, [0, [7, 11.0, None, 'captured trailing field']]])
            self.assertIsInstance(result[1][1][1], float)

    async def test_recovered_account_uses_its_own_capture(self):
        original = [[0, [7, 10.0, 'recovered trailing field']], None]
        recovered = {'snapshot': b''.join(pack(p) for p in ([], original, [], [], [])),
                     'baseline': pack([[0, [7, 10]]])}
        result = await self.snapshot(Path('/unused'), {'user_id': 1, 'mode': 'fresh'}, recovered)
        self.assertEqual(result, [[0, [7, 11.0, 'recovered trailing field']], None])

    async def test_fresh_account_needs_no_recovery_tables_or_capture_files(self):
        result = await self.snapshot(Path('/unused'), {'user_id': 7, 'mode': 'fresh'},
                                     table_exists=False)
        self.assertEqual(result, [[0, [7, 11]]])
