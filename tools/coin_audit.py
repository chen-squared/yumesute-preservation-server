"""Diagnostics: record every write to currency.coin.

The coin balance of the recovered account jumped far past anything the local reward tables
can hand out, so this pins down the writer instead of guessing. Every statement that touches
currency.coin is bracketed by a read of the balance and reported as old -> new with the
request that ran it. Nothing here changes a value or rejects a request.
"""
from contextvars import ContextVar

from db import DBConnWrapper

# Set by the request middleware below; statements outside a request (startup, background
# jobs) report "-".
current_request = ContextVar("coin_audit_request", default="-")

# add_currency (coin = coin + n), upsert_currency (INSERT) and State.update('Currency').
_HEADS = ('update "currency"', 'insert into "currency"')


def touches_coin(sql):
    text = " ".join(str(sql).lower().split())
    return '"coin"' in text and any(head in text for head in _HEADS)


async def balances(conn):
    rows = await conn.fetch('SELECT "userId", "coin" FROM "currency"')
    return {row["userId"]: row["coin"] for row in rows}


def movement(before, after):
    """(userId, old, new) for every balance that changed."""
    for uid in sorted(set(before) | set(after)):
        old, new = before.get(uid), after.get(uid)
        if old != new:
            yield uid, old, new


def report(before, after, sql):
    label = current_request.get()
    head = " ".join(str(sql).split())[:100]
    for uid, old, new in movement(before, after):
        print(
            f'coin-audit {label} uid={uid} {old} -> {new} '
            f'delta={int(new or 0) - int(old or 0):+d} sql={head}',
            flush=True,
        )


async def bracketed(conn, sqls, run):
    """Run `run`, logging the coin movement it caused if any statement writes coin."""
    if not any(touches_coin(sql) for sql in sqls):
        return await run()
    try:
        before = await balances(conn)
    except Exception:  # never let diagnostics break a request
        return await run()
    result = await run()
    try:
        report(before, await balances(conn), " ; ".join(str(s) for s in sqls))
    except Exception:
        pass
    return result


def install(app):
    """Wrap the DB connection's executable calls and note the current request path."""
    if getattr(DBConnWrapper, "_coin_audit", False):
        return
    DBConnWrapper._coin_audit = True

    execute, execute_all, execute_batch = (
        DBConnWrapper.execute,
        DBConnWrapper.executeAll,
        DBConnWrapper.execute_batch,
    )

    async def audited_execute(self, query):
        return await bracketed(self.conn, [query.sql], lambda: execute(self, query))

    async def audited_execute_all(self, queries):
        sqls = [q.sql for q in queries]
        return await bracketed(self.conn, sqls, lambda: execute_all(self, queries))

    async def audited_execute_batch(self, sql, args_seq):
        return await bracketed(self.conn, [sql], lambda: execute_batch(self, sql, args_seq))

    DBConnWrapper.execute = audited_execute
    DBConnWrapper.executeAll = audited_execute_all
    DBConnWrapper.execute_batch = audited_execute_batch

    @app.middleware("http")
    async def note_request(request, call_next):
        token = current_request.set(f"{request.method} {request.url.path}")
        try:
            return await call_next(request)
        finally:
            current_request.reset(token)
