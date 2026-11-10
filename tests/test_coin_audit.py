"""Coin audit detection: the three statements that can move currency.coin are caught,
reward writes to other tables are not, and a caught statement is bracketed by the two
balance reads that produce the log line. SQL shapes come from db/user/create.py
(add_currency, upsert_currency) and the local State.update('Currency', ...).
"""
import asyncio
from contextlib import redirect_stdout
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "vendor/server-of-dreams"))

from coin_audit import bracketed, movement, touches_coin
from db.user import add_currency


class FakeConn:
    """Stands in for DBConnWrapper.conn: one account, one mutable balance."""

    def __init__(self):
        self.coin = 100
        self.reads = 0

    async def fetch(self, sql):
        self.reads += 1
        return [{"userId": 7, "coin": self.coin}]


class TestCoinAudit(unittest.TestCase):
    def test_incremental_grant_is_caught(self):
        self.assertTrue(touches_coin(add_currency(1, coin=100).sql))
        self.assertTrue(touches_coin(add_currency(1, free_jewel=100).sql))

    def test_absolute_writes_are_caught(self):
        update = 'UPDATE "currency" SET "coin"=$3 WHERE "userId"=$1 AND "id"=$2'
        insert = (
            'INSERT INTO "currency" ("userId", "id", "coin", "freeJewel", "paidJewel") '
            "VALUES ($1, $2, $3, $4, $5)"
        )
        self.assertTrue(touches_coin(update))
        self.assertTrue(touches_coin(insert))

    def test_other_tables_are_left_alone(self):
        self.assertFalse(
            touches_coin('UPDATE "item" SET "stock"=$3 WHERE "userId"=$1 AND "id"=$2')
        )
        self.assertFalse(
            touches_coin('SELECT "coin" FROM "currency" WHERE "userId"=$1')
        )
        self.assertFalse(
            touches_coin('UPDATE "user" SET "currentStamina"=$3 WHERE "userId"=$1')
        )

    def test_report_only_lists_balances_that_moved(self):
        self.assertEqual(
            list(movement({7: 100, 8: 5}, {7: 250, 8: 5})), [(7, 100, 250)]
        )
        self.assertEqual(list(movement({7: 100}, {7: 100})), [])
        # A balance that appears (or disappears) mid-request still counts as a write.
        self.assertEqual(list(movement({}, {9: 42})), [(9, None, 42)])

    def test_coin_statement_is_bracketed_and_logged(self):
        conn, out = FakeConn(), io.StringIO()
        sql = add_currency(7, coin=250).sql

        async def write():
            conn.coin += 250
            return "written"

        with redirect_stdout(out):
            result = asyncio.run(bracketed(conn, [sql], write))
        self.assertEqual(result, "written")
        self.assertEqual(conn.reads, 2)  # balance before and after
        self.assertIn('coin-audit - uid=7 100 -> 350 delta=+250', out.getvalue())
        self.assertIn('UPDATE "currency"', out.getvalue())

    def test_other_statements_cost_nothing_extra(self):
        conn = FakeConn()

        async def write():
            conn.coin += 1
            return "written"

        sql = 'UPDATE "item" SET "stock" = "stock" + $2 WHERE "userId" = $1'
        self.assertEqual(asyncio.run(bracketed(conn, [sql], write)), "written")
        self.assertEqual(conn.reads, 0)


if __name__ == "__main__":
    unittest.main()
