"""Stamina/gift route checks; requires a disposable yumesute_review_checks_<suffix> clone."""
import argparse
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools'), str(ROOT / 'vendor/server-of-dreams')]


async def main(name):
    if not name.startswith('yumesute_review_checks_') or not name.isidentifier():
        raise SystemExit('Only a disposable yumesute_review_checks_<suffix> database is allowed')
    from helpers.config import config, database
    config['database']['database'] = database.database = name
    import httpx
    import gameplay
    import progression
    import serve
    import stamina_compat
    from db.user import upsert_inbox
    from helpers.auth import make_session_jwt
    from helpers.msgpack import from_array, pack
    from helpers.stamina import effective_stamina, max_stamina, recovery_micros, restored_at
    from helpers.things import grant_thing, grant_things_consolidated
    from models import BulkReceivePayload, PurchaseItemPayload
    from models.enums import ThingTypes
    from models.unions import IDATA_OBJECT_KEY
    from protocol import expand, unpack_stream

    stamina_type = int(ThingTypes.Stamina)
    app = serve.app
    async with app.router.lifespan_context(app):
        async with app.acquire_db() as c:
            assert await c.conn.fetchval('SELECT current_database()') == name
            uid = await c.conn.fetchval('SELECT "userId" FROM "character" GROUP BY "userId" '
                                        'ORDER BY count(*) DESC LIMIT 1')
            assert uid, 'Copy a prepared account into this disposable database'
        token = make_session_jwt(uid, 'AppStore')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost',
                                    headers={'Authorization': f'Bearer {token}'}) as client:
            async def post(path, payload=None, valid=True):
                r = await client.post(path, content=pack(payload) if payload is not None else b'')
                assert r.status_code == 200, (path, r.status_code)
                parts = [expand(v) for v in unpack_stream(r.content)]
                assert bool(parts[0]) != valid, (path, parts[0])
                return parts

            async def balance():
                async with app.acquire_db() as c:
                    return dict(await c.conn.fetchrow('SELECT * FROM "user" WHERE "userId"=$1', uid))

            async def coins():
                async with app.acquire_db() as c:
                    return await c.conn.fetchval('SELECT coin FROM currency WHERE "userId"=$1', uid)

            async def gifts():
                async with app.acquire_db() as c:
                    return [dict(r) for r in await c.conn.fetch('SELECT * FROM inbox WHERE "userId"=$1 '
                            'AND "thingType"=$2 ORDER BY id', uid, stamina_type)]

            async def reset(held, *, full_at=0):
                async with app.acquire_db() as c:
                    await c.conn.execute('DELETE FROM inbox WHERE "userId"=$1 AND "thingType"=$2', uid, stamina_type)
                    await c.conn.execute('UPDATE "user" SET "playerRank"=100,"playerRankLimit"=150,'
                        '"currentRankPoint"=0,"currentStamina"=$2,"maxStaminaRestoredAt"=$3 '
                        'WHERE "userId"=$1', uid, held, full_at)
                    await c.conn.execute('UPDATE jewel_shop SET "purchaseCount"=0,"rePurchaseDate"=0 '
                                        'WHERE "userId"=$1 AND "jewelShopItemMasterId"=4101', uid)

            async def set_held(held):
                async with app.acquire_db() as c:
                    await c.conn.execute('UPDATE "user" SET "currentStamina"=$2,"maxStaminaRestoredAt"=0 '
                                        'WHERE "userId"=$1', uid, held)

            async def gift(quantity, kind=ThingTypes.Stamina):
                async with app.acquire_db() as c, c.transaction():
                    await c.conn.execute('SELECT pg_advisory_xact_lock($1::bigint)', uid)
                    ident = await c.conn.fetchval('SELECT COALESCE(MAX(id),0)+1 FROM inbox WHERE "userId"=$1', uid)
                    values = next(stamina_compat.overflow_packages(1, time.time_ns() // 1000))
                    values.update(id=ident, thingType=int(kind), thingQuantity=quantity)
                    await c.execute(upsert_inbox(uid, values))
                    return ident

            async def receive(ident):
                parts = await post(f'/api/Inboxes/{ident}/Receive')
                return from_array('InboxReceiveResult', parts[1])

            async def buy():
                return await post('/api/Shops/Purchase', PurchaseItemPayload(m_jewel_shop_item_id=4101))

            await reset(2990)
            before_coin = await coins()
            paid = await buy()
            receipts = [from_array('ReceivedThing', r) for r in paid[1]]
            assert [(r.quantity, r.sent_inbox) for r in receipts if int(r.type) == stamina_type] == [(10, False), (50, True)]
            pending = await gifts()
            assert len(pending) == 1 and pending[0]['thingQuantity'] == 50
            assert any(tag == IDATA_OBJECT_KEY['Inbox'] and values[0] == pending[0]['id']
                       for tag, values in paid[2]), 'Pack overflow missing from client present'
            assert not pending[0]['hasReceived'] and not pending[0]['checked']
            assert not pending[0]['isTimeLimited'] and pending[0]['receiveLimitAt'] == stamina_compat.NO_EXPIRY
            assert (await balance())['currentStamina'] == 3000 and await coins() == before_coin + 10000
            await post('/api/Shops/Purchase', PurchaseItemPayload(m_jewel_shop_item_id=4101), valid=False)
            assert await gifts() == pending, 'A duplicate purchase created another overflow gift'
            print('PASS: pack credits 10 stamina, preserves overflow 50 as a permanent gift, and pays once')

            ident = pending[0]['id']
            for held in (3000, 2951):
                await set_held(held)
                result = await receive(ident)
                assert result.has_not_receive_things and not result.received_things
                assert (await balance())['currentStamina'] == held and await gifts() == pending
            await set_held(2950)
            result = await receive(ident)
            assert not result.has_not_receive_things and len(result.received_things) == 1
            assert result.received_things[0].quantity == 50 and not result.received_things[0].sent_inbox
            assert (await balance())['currentStamina'] == 3000 and (await gifts())[0]['hasReceived']
            assert not (await receive(ident)).received_things
            print('PASS: a gift stays whole until it fits; exactly 3000 is allowed; repeat claim pays nothing')

            now = time.time_ns() // 1000
            natural_max = max_stamina(100)
            full_at = now + (natural_max - 30) * recovery_micros()
            await reset(10, full_at=full_at)
            with patch('stamina_compat.time.time_ns', return_value=now * 1000):
                await buy()
            current = await balance()
            assert current['currentStamina'] == 90
            assert current['maxStaminaRestoredAt'] == restored_at(90, natural_max, now)
            await reset(10, full_at=full_at)
            too_large = await gift(2980)
            with patch('stamina_compat.time.time_ns', return_value=now * 1000):
                refused = await receive(too_large)
            assert refused.has_not_receive_things and not refused.received_things
            assert not (await gifts())[0]['hasReceived'] and (await balance())['currentStamina'] == 10
            print('PASS: rewards and gift capacity include natural recovery before calculating the new balance')

            await reset(1200)
            await buy()
            current = await balance()
            assert current['currentStamina'] == 1260 and not await gifts()
            assert effective_stamina(1260, current['maxStaminaRestoredAt'], natural_max,
                                     current['maxStaminaRestoredAt'] + 86400_000000) == 1260
            await reset(3000)
            await buy()
            assert (await balance())['currentStamina'] == 3000
            assert [g['thingQuantity'] for g in await gifts()] == [60]
            print('PASS: stamina may exceed the natural maximum without regeneration; a full stock defers all 60')

            await reset(2990)
            async with app.acquire_db() as c, c.transaction():
                await c.conn.execute('SELECT pg_advisory_xact_lock($1::bigint)', uid)
                state = gameplay.State(c, uid)
                received = await state.grant([(stamina_type, 0, 60), (stamina_type, 0, 40)])
                assert [(r.quantity, r.sent_inbox) for r in received] == [(10, False), (90, True)]
                assert any(tag == IDATA_OBJECT_KEY['Inbox'] for tag, _ in state.present())
                assert (await state.one('User'))['currentStamina'] == 3000
            assert [g['thingQuantity'] for g in await gifts()] == [90]
            print('PASS: progression rewards consolidate stamina and publish the updated user and overflow inbox')

            await reset(2990)
            async with app.acquire_db() as c:
                received = await grant_things_consolidated(c, uid, [(stamina_type, 0, 6100)])
            pending = await gifts()
            assert [g['thingQuantity'] for g in pending] == [3000, 3000, 90]
            assert sum(r.quantity for r in received) == 6100
            assert (await balance())['currentStamina'] + sum(g['thingQuantity'] for g in pending) == 9090
            await reset(3000)
            async with app.acquire_db() as c:
                received = await grant_thing(c, uid, stamina_type, 0, 60)
            assert received.sent_inbox and received.quantity == 60
            assert [g['thingQuantity'] for g in await gifts()] == [60]
            print('PASS: consolidated and single rewards preserve overflow; large rewards make individually claimable gifts')

            for held in (2900, 3000):
                await reset(held)
                levels = {r.rank: r for r in gameplay.cache.player_rank_master}
                experience = sum(levels[r].point_to_level_up for r in range(100, 120))
                async with app.acquire_db() as c, c.transaction():
                    await c.conn.execute('SELECT pg_advisory_xact_lock($1::bigint)', uid)
                    state = gameplay.State(c, uid)
                    await progression.rank_xp(state, experience)
                    assert any(tag == IDATA_OBJECT_KEY['Inbox'] for tag, _ in state.present()), 'Overflow inbox missing from rank-up present'
                total_restore = sum(max_stamina(r) for r in range(101, 121))
                pending = await gifts()
                current = await balance()
                assert current['playerRank'] == 120 and current['currentStamina'] == 3000
                assert sum(g['thingQuantity'] for g in pending) == held + total_restore - 3000
                assert all(g['thingQuantity'] <= 3000 for g in pending)
            print('PASS: multi-level rank-up preserves every point of restored stamina and publishes overflow gifts')

            await reset(2950)
            large, small, coin_gift = await gift(60), await gift(20), await gift(12345, ThingTypes.Coin)
            before_coin = await coins()
            result = await post('/api/Inboxes/BulkReceive', BulkReceivePayload(inbox_ids=[large, small, coin_gift, small]))
            result = from_array('InboxReceiveResult', result[1])
            assert result.has_not_receive_things
            assert sorted((int(r.type), r.quantity) for r in result.received_things) == sorted([(stamina_type, 20), (int(ThingTypes.Coin), 12345)])
            assert (await balance())['currentStamina'] == 2970 and await coins() == before_coin + 12345
            pending = await gifts()
            assert len(pending) == 2 and not pending[0]['hasReceived'] and pending[1]['hasReceived']
            print('PASS: bulk receive skips gifts that do not fit and still claims fitting stamina/other rewards once')

            await reset(2920)
            first, second = await gift(60), await gift(60)
            results = await asyncio.gather(receive(first), receive(second), receive(first), receive(second))
            assert sum(len(r.received_things or []) for r in results) == 1
            pending = await gifts()
            assert sum(g['hasReceived'] for g in pending) == 1 and len(pending) == 2
            assert (await balance())['currentStamina'] == 2980
            print('PASS: concurrent and duplicate claims share the capacity check and pay exactly once')

            await reset(2990)
            before_coin = await coins()
            async with app.acquire_db() as c:
                with patch('stamina_compat.upsert_inbox', side_effect=RuntimeError('injected gift failure')):
                    try:
                        await grant_things_consolidated(c, uid, [(int(ThingTypes.Coin), 0, 10000), (stamina_type, 0, 60)])
                    except RuntimeError as exc:
                        assert str(exc) == 'injected gift failure'
                    else:
                        raise AssertionError('Injected overflow failure did not abort')
            assert (await balance())['currentStamina'] == 2990 and await coins() == before_coin and not await gifts()
            print('PASS: a failed gift insert rolls back both the stamina and the other pack rewards')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    asyncio.run(main(parser.parse_args().database))
