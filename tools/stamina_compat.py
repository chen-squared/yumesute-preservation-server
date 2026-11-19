"""Stamina rewards: keep 3000 on hand and preserve overflow in claimable gifts."""
import sys
import time

from db import ExecutableQuery
from db.user import upsert_inbox
from helpers import things
from helpers.stamina import effective_stamina, max_stamina, restored_at
from models import ReceivedThing
from models.enums import ThingTypes

STAMINA_LIMIT = 3000
# The captured permanent-gift deadline; zero can be filtered as expired by the client.
NO_EXPIRY = 4102358400000000
_grant_one = things.grant_thing
_grant_many = things.grant_things_consolidated
_installed = False


def overflow_packages(amount, now, thing_id=0):
    """Split large overflow so each gift can eventually be received in full."""
    while amount > 0:
        quantity = min(STAMINA_LIMIT, amount)
        yield dict(thingType=int(ThingTypes.Stamina), thingId=thing_id,
                   thingQuantity=quantity, isTimeLimited=False, hasReceived=False,
                   title='スタミナ超過分', description='所持上限を超えたスタミナ',
                   sentAt=now, receivedAt=None, receiveLimitAt=NO_EXPIRY)
        amount -= quantity


async def _change(conn, uid, amount, *, strict, now=None, thing_id=0):
    if amount < 0:
        raise ValueError('Stamina rewards must be nonnegative')
    async with conn.transaction():
        await conn.conn.execute('SELECT pg_advisory_xact_lock($1::bigint)', uid)
        row = await conn.conn.fetchrow('SELECT * FROM "user" WHERE "userId"=$1 FOR UPDATE', uid)
        if row is None:
            raise ValueError('Missing stamina account')
        now = time.time_ns() // 1000 if now is None else now
        natural_max = max_stamina(row['playerRank'])
        current = effective_stamina(row['currentStamina'], row['maxStaminaRestoredAt'],
                                    natural_max, now)
        total = current + amount
        if strict and total > STAMINA_LIMIT:
            return None
        balance = min(STAMINA_LIMIT, total)
        overflow = total - balance
        await conn.execute(ExecutableQuery(
            'UPDATE "user" SET "currentStamina"=$2,"maxStaminaRestoredAt"=$3 WHERE "userId"=$1',
            uid, balance, restored_at(balance, natural_max, now)))
        for package in overflow_packages(overflow, now, thing_id):
            ident = await conn.conn.fetchval('SELECT COALESCE(MAX(id),0)+1 FROM inbox WHERE "userId"=$1', uid)
            await conn.execute(upsert_inbox(uid, dict(id=ident, **package)))
        return max(0, balance - current), overflow


async def grant_stamina(conn, uid, amount, thing_id=0):
    direct, overflow = await _change(conn, uid, amount, strict=False, thing_id=thing_id)
    result = []
    if direct or not overflow:
        result.append(ReceivedThing(type=ThingTypes.Stamina, id_=thing_id,
                                    quantity=direct, sent_inbox=False))
    if overflow:
        result.append(ReceivedThing(type=ThingTypes.Stamina, id_=thing_id,
                                    quantity=overflow, sent_inbox=True))
    return result


async def receive_stamina(conn, uid, amount, now=None):
    """A gift is all or nothing; rejection never creates a replacement gift."""
    return await _change(conn, uid, amount, strict=True, now=now) is not None


async def grant_thing(conn, uid, thing_type, thing_id, quantity):
    if int(thing_type) != int(ThingTypes.Stamina):
        return await _grant_one(conn, uid, thing_type, thing_id, quantity)
    received = await grant_stamina(conn, uid, quantity, thing_id)
    # Single-thing callers have one receipt: retain the whole awarded quantity and
    # indicate deferred stamina. List callers below report both destinations separately.
    return ReceivedThing(type=ThingTypes.Stamina, id_=thing_id, quantity=quantity,
                         sent_inbox=any(r.sent_inbox for r in received))


async def grant_things_consolidated(conn, uid, rewards):
    rewards = [(int(t), int(i), int(q)) for t, i, q in rewards]
    stamina = {}
    for kind, ident, quantity in rewards:
        if int(kind) == int(ThingTypes.Stamina):
            stamina[ident] = stamina.get(ident, 0) + quantity
    if not stamina:
        return await _grant_many(conn, uid, rewards)
    async with conn.transaction():
        await conn.conn.execute('SELECT pg_advisory_xact_lock($1::bigint)', uid)
        received = await _grant_many(conn, uid, [r for r in rewards if int(r[0]) != int(ThingTypes.Stamina)])
        for ident, amount in stamina.items():
            received.extend(await grant_stamina(conn, uid, amount, ident))
    # Preserve the vendor's stackable-first ordering and each resource's first occurrence.
    sequence = list(dict.fromkeys((int(t), i) for t, i, _ in rewards))
    stackable = {int(t) for t in (ThingTypes.Coin, ThingTypes.Jewel, ThingTypes.Item, ThingTypes.Stamina)}
    positions = {key: (key[0] not in stackable, n) for n, key in enumerate(sequence)}
    received.sort(key=lambda r: positions[(int(r.type), r.id_)])
    return received


def install():
    """Replace the pinned vendor's grant entry points, including imported aliases."""
    global _installed
    if _installed:
        return
    _installed = True
    for name in ('helpers.things', 'helpers.live_drops', 'routes.characters', 'routes.episodes',
                 'routes.gachas', 'routes.inbox', 'routes.shops', 'gameplay', 'reroll',
                 'jewel_shop', 'preservation_gift'):
        module = sys.modules.get(name)
        if module is None:
            continue
        for attr, before, after in (('grant_thing', _grant_one, grant_thing),
                                    ('grant_things_consolidated', _grant_many, grant_things_consolidated)):
            if getattr(module, attr, None) is before:
                setattr(module, attr, after)
