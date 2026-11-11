"""What one jewel-shop purchase hands over, and how often it may happen.

``routes/shops.py`` answered every ``Shops/Purchase`` with ``give_paid_jewel``, which is the
ledger of paid jewels a real-money pack stands for - not the pack's contents. All 25 free
rows (``ShopPurchaseTypes.Free``) carry ``give_paid_jewel = null``, so 毎日無料パック and the
カスタマイズパック / イベント限定ジュエルパック【FREE】 rows delivered nothing at all, while
MissionMaster 100400 exists only to reward taking the first of them. A pack's contents are
its ``lineup``, so a free purchase now hands that over: 4101 is スタミナ60 + コイン10000.

The paid rows keep upstream's grant, because that is a different question with its own
blocker. On the 1895 paid rows that carry a lineup, ``give_paid_jewel`` is the money's worth
and the lineup is what the game actually sends (11 【1回限定】劇ジュエルパックA: 120 paid
jewels, lineup 130; the commemorative packs put tickets beside the jewels) - and this server
has no way to put a jewel in ``paidJewel`` instead of ``freeJewel``, which is the TODO next
to upstream's own grant. So the free rows are fixed and the paid rows are
recorded here as still open rather than answered with the wrong ledger.

The cap is the other half. Every one of those 25 free rows says ``purchase_limit`` 1, and
nothing checked it - while the recovered save shows what the official server did: item 4101
at ``purchaseCount`` 1, ``totalPurchaseCount`` 1044 (about one per day across the account's
life) and ``rePurchaseDate`` 1786078800000000. That stamp is the instant the pack could be
bought again, and in the wall-clock hours the master itself uses (event windows close at
17:00 and 22:00) its hour is 05 - the same boundary ``helpers/daily.py`` resets the daily
allowances on, and the hour the daily and weekly mission rows open at.

So ``replace_types`` (``ShopReplaceTypes``) is read as that calendar: ``None`` is a lifetime
cap, Daily/Weekly/Monthly reset on the 05:00 boundaries ``missions.cycle_start`` already
uses for the mission cycles. ``DailyPassExpired`` is left alone on purpose: it rolls when a
daily pass expires, and passes are not modelled on this server, so that row keeps
upstream's uncapped behaviour rather than inventing a rule for it.

The cap is enforced inside the claim statement's own ``WHERE`` clause, the way
``db/user/update.py consume_exchange_limit`` does it, so two taps cannot both slip through a
read-then-write gap.
"""
import datetime
import random
import time

from fastapi import APIRouter, Request
from helpers.daily import most_recent_reset
from helpers.msgpack import fault, read_request, respond
from helpers.shops import available, jewel_shop_item
from helpers.things import grant_things_consolidated, present_type
from helpers.user_data import build_present, current_user_id
from models import PurchaseItemPayload
from models.enums import ShopPurchaseTypes, ShopReplaceTypes, ThingTypes

from gameplay import transaction

router = APIRouter()

_MICRO = 1_000_000
_DAY = 86_400 * _MICRO
_JST = datetime.timezone(datetime.timedelta(hours=9))

# ShopReplaceTypes that roll over on a calendar clock. DailyPassExpired (10) is absent: it
# depends on a pass's expiry, and this server does not track passes.
_CALENDAR_PERIODS = {
    int(ShopReplaceTypes.Daily): "daily",
    int(ShopReplaceTypes.Weekly): "weekly",
    int(ShopReplaceTypes.Monthly): "monthly",
}
LIFETIME = int(ShopReplaceTypes.None_)

FREE = int(ShopPurchaseTypes.Free)


def period_end(replace_types, now: int):
    """Epoch-micros when a pack's purchase cap resets, or None for a lifetime cap.

    Daily, weekly and monthly all roll on the game's 05:00 boundaries, and the weekly phase
    is the one the weekly mission rows opened on, so the shop's clock and the mission
    cycle's cannot drift apart. An unrecognised replace type also answers None.
    """
    kind = _CALENDAR_PERIODS.get(int(replace_types or 0))
    if kind is None:
        return None
    if kind == "monthly":
        # The hour comes from the daily boundary itself, so the month rolls on the same
        # 05:00 instants the day and the week do.
        today = datetime.datetime.fromtimestamp(most_recent_reset(now) / _MICRO, _JST)
        first_of_next = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        return int(first_of_next.timestamp() * _MICRO)
    import missions
    days = 1 if kind == "daily" else 7
    category = missions.DAILY if kind == "daily" else missions.WEEKLY
    return missions.cycle_start(category, now) + days * _DAY


def lineup(item):
    """A pack's contents as ``(thing_type, thing_id, quantity)`` triples, in master order."""
    return [(int(line.thing_type), int(line.thing_id or 0), int(line.thing_quantity or 0))
            for line in item.lineup or [] if int(line.thing_quantity or 0) > 0]


def granted(item):
    """What one purchase of ``item`` hands over.

    A free pack is its lineup. A paid row still gets ``give_paid_jewel``, as upstream
    granted it: the lineup is the real gift there, but it cannot be paid out until
    ``helpers/things.py`` can put jewels in the ``paidJewel`` column rather than the
    ``freeJewel`` one, which is that route's own TODO. 6 of the 1926 rows (the three plain
    劇ジュエルパックA rows and the three passes) have no lineup at all.
    """
    if int(item.purchase_type) == FREE:
        return lineup(item)
    return [(int(ThingTypes.Jewel), 0, int(item.give_paid_jewel or 0))]


def claim_purchase(user_id: int, item_id: int, limit: int, again, now: int, new_id: int):
    """One slot against the pack's cap, creating the row on first purchase.

    ``(sql, args)`` for a statement that returns a row when the slot was taken and nothing
    when the cap is used up, so the caller can reject the purchase before granting.
    ``again`` is when the cap resets (None for a lifetime cap) and is stored as
    ``rePurchaseDate``. A period pack whose stamp is still NULL was recorded before this
    server stamped instants, so its count is treated as a spent period; a lifetime pack's
    NULL stamp never expires.
    """
    stale = 0 if again is not None else 2 ** 62
    expired = f'COALESCE("rePurchaseDate", {stale}) <= $5'
    counted = f'CASE WHEN {expired} THEN 1 ELSE "purchaseCount" + 1 END'
    sql = (
        'WITH upd AS ('
        f'  UPDATE "jewel_shop" SET "purchaseCount" = {counted}, '
        '    "totalPurchaseCount" = "totalPurchaseCount" + 1, "rePurchaseDate" = $4 '
        '  WHERE "userId" = $1 AND "jewelShopItemMasterId" = $2 '
        f'    AND ({counted}) <= $3 '
        "  RETURNING 1"
        "), ins AS ("
        '  INSERT INTO "jewel_shop" ("userId", "id", "jewelShopItemMasterId", '
        '    "purchaseCount", "totalPurchaseCount", "rePurchaseDate") '
        "  SELECT $1, $6, $2, 1, 1, $4 "
        "  WHERE NOT EXISTS (SELECT 1 FROM upd) "
        '    AND NOT EXISTS (SELECT 1 FROM "jewel_shop" '
        '      WHERE "userId" = $1 AND "jewelShopItemMasterId" = $2) '
        "    AND 1 <= $3 "
        "  RETURNING 1"
        ") SELECT 1 FROM upd UNION ALL SELECT 1 FROM ins"
    )
    return sql, (user_id, item_id, limit, again, now, new_id)


@router.post("/api/Shops/Purchase")
async def purchase(request: Request):
    """One jewel-shop pack: its own lineup, at most as often as its own cap allows.

    This replaces the upstream route outright instead of wrapping it, because both halves
    of the fix need the master row and the account's purchase row in the same transaction
    as the grant. The guards upstream ran come with it: no user or no payload answers an
    empty result, an unknown row answers empty, and a row outside its sale window answers
    ``OutOfPeriod``. Paid rows (``purchase_type`` 1/2) are still granted without a store
    receipt - the same note ``routes/shops.py`` carries about ``KmsGeneralPayment`` - and
    still land in ``freeJewel``, that route's own TODO.
    """
    app = request.app
    user_id = current_user_id(request)
    payload = await read_request(request, PurchaseItemPayload)
    if user_id is None or payload is None:
        return respond([])

    item = jewel_shop_item(payload.m_jewel_shop_item_id)
    if item is None:
        return respond([])
    if not available(item.start_date, item.end_date):
        return respond([], faults=[fault("OutOfPeriod")])

    now = time.time_ns() // 1000          # epoch microseconds, the unit every shop helper takes
    replace = int(item.replace_types or 0)
    limit = int(item.purchase_limit or 0)
    if not limit or (replace != LIFETIME and replace not in _CALENDAR_PERIODS):
        limit, again = 0, None           # no cap, or a cap this server cannot read
    else:
        again = period_end(replace, now)

    import missions                      # 100400 ジュエルショップの「毎日無料パック」を受け取ろう
    async with transaction(request) as s:
        if limit:
            sql, args = claim_purchase(s.uid, item.id_, limit, again, now,
                                       random.randint(1_000_000, 9_999_999_999))
            if not await s.conn.conn.fetchrow(sql, *args):
                return respond([], faults=[fault("PurchaseLimitExceeded")])
        received = await grant_things_consolidated(s.conn, s.uid, granted(item))
        if item.id_ == missions.FREE_PACK_ITEM:
            await missions.free_pack(s)
        counted = s.present()
        refresh = {present_type(int(r.type)) for r in received} | {"JewelShop"}
        refresh.discard(None)

    present = await build_present(app, user_id, *sorted(refresh))
    return respond(received, present=present + counted)


def install(app):
    """Replace the shop's purchase route. Nothing to create: ``jewel_shop`` already exists."""
    count = len(app.router.routes)
    app.include_router(router)
    app.router.routes[:] = app.router.routes[count:] + app.router.routes[:count]
