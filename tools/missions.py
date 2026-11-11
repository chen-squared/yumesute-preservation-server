"""The daily and weekly missions: re-issued on their own clocks, counted where they happen.

MissionMaster keeps both cycles as ordinary missions and its ``description`` states each
condition. Category 4 (``MissionCategories.Daily``) is 100000 スタミナを300消費, 100100
フォトを1枚現像, 100200 稽古を1回, 100300 全てのデイリーミッションをクリア and 100400
ジュエルショップの毎日無料パック; category 5 (``MissionCategories.Weekly``) is 200000
スタミナを3000消費, 200100 演劇リーグを1回, 200200 スターアクトを5回, 200300 フォトを3枚現像,
200400 フォトのレベルを合計3, 200500 マーケット（レギュラー）で10回交換 and 200600
「全てのデイリーミッションをクリアしよう」を5回. Three were already counted before this
module existed (100200 in progression, 200300 in photo_preservation, 200100 in leagues);
the rest are counted here, at the point where the game really spends or produces the thing
the mission asks for, and nowhere else. 100400 is the exception in placement only: its
counter is here, but the route that triggers it is ``jewel_shop.purchase``, which also has
to hand the pack its lineup and keep it to once a day.

Two gaps are left visible rather than papered over:

* 100100 (daily 現像) closed on 2023-10-11 and stays closed. That is also why 100300's goal
  is 3 and not 4: three daily missions are still on sale besides itself.
* 200400 (photo levels) is left unprogressed. Photo enhancement, ``Photos/PhotoLevelUp``, is
  still an unimplemented stub upstream, so no photo level can rise on this server and there
  is nothing true to count. No counter is wired to a route that changes nothing.

The clocks come from the archive. Master timestamps are serialized with a ``+00:00`` offset
but carry JST wall-clock hours - event windows close at 17:00 and 22:00, which are closing
times rather than 02:00 or 07:00 - and read that way the mission rows start their cycle at
05:00: the daily rows open 2023-08-26T05:00, the weekly rows 2023-10-16T05:00, a Monday.
That is the same hour at which ``helpers/daily.py`` resets the daily allowances, and no
capture pins a different weekly instant, so the day is that helper's 05:00 JST boundary and
the week is seven days from the Monday the weekly rows opened on. Both live in
``cycle_start``, so a capture that says otherwise corrects them in one place.
"""
import datetime
import time

from helpers.cache import cache
from helpers.daily import most_recent_reset
from helpers.shops import available
from routes import shops as upstream_shops

from gameplay import State, mission_progress

_JST = datetime.timezone(datetime.timedelta(hours=9))
_MICRO = 1_000_000
_DAY = 86400 * _MICRO

# MissionCategories in the client (dump.cs): Daily = 4, Weekly = 5.
DAILY, WEEKLY = 4, 5

# The mission rows themselves, named in MissionMaster's descriptions.
STAMINA = {DAILY: 100000, WEEKLY: 200000}
STAR_ACTS = 200200
MARKET_EXCHANGES = 200500
FREE_PACK = 100400
ALL_DAILIES = 100300
ALL_DAILIES_WEEK = 200600

# JewelShopItemMaster 4101 毎日無料パック: the only free (``purchase_type`` 3) pack in the
# お得パック category whose name says 毎日, and the only jewel-shop row carrying that word.
# Mission 100400 jumps to that category (``jump_type`` 3, ``jump_target_id`` 4).
FREE_PACK_ITEM = 4101

CYCLE_TABLE = "preservation_mission_cycle"


async def ensure_cycle_table(conn) -> None:
    """The local marker saying which cycle the account's mission rows belong to."""
    await conn.conn.execute(
        f'CREATE TABLE IF NOT EXISTS {CYCLE_TABLE} ('
        '"userId" bigint NOT NULL REFERENCES accounts("userId") ON DELETE CASCADE, '
        '"missionCategory" integer NOT NULL, '
        '"cycleStartAt" bigint NOT NULL, '
        'PRIMARY KEY ("userId","missionCategory"))')


def cycle_start(category: int, now: int) -> int:
    """Epoch-micros of the cycle ``now`` falls in, for a daily or weekly category."""
    day = most_recent_reset(now)
    if category == DAILY:
        return day
    weekday = datetime.datetime.fromtimestamp(day / _MICRO, _JST).weekday()
    return day - weekday * _DAY


def on_sale(row, now: float) -> bool:
    """Whether a mission master row is on sale at ``now`` (epoch micros)."""
    return bool(available(row.start_date, row.end_date,
                          datetime.datetime.fromtimestamp(now / _MICRO, datetime.timezone.utc)))


def issued(category: int, now: int):
    """The mission rows this category has to have in the current cycle."""
    return [m for m in cache.mission_master
            if int(m.mission_category) == category and m.stages and on_sale(m, now)]


def current_stage(mission, now: int):
    """The stage a fresh issue of ``mission`` starts on: the latest one already started."""
    stages = sorted(mission.stages or [], key=lambda x: x.mission_stage_order)
    started = [x for x in stages if available(x.start_date, None,
              datetime.datetime.fromtimestamp(now / _MICRO, datetime.timezone.utc))]
    return started[-1] if started else stages[0]


async def rotate(s, now: int | None = None) -> None:
    """Re-issue the daily and weekly missions when the account crosses into a new cycle.

    The official server reissued these rows on its own clock and left no marker behind, so
    the marker is local: one row per account per category, holding the cycle it was last
    issued for. Only missions that are on sale are touched - a closed one such as 100100
    keeps whatever the recovered save carried.
    """
    now = now if now is not None else time.time_ns() // 1000
    for category in (DAILY, WEEKLY):
        start = cycle_start(category, now)
        stored = await s.conn.conn.fetchval(
            f'SELECT "cycleStartAt" FROM {CYCLE_TABLE} WHERE "userId"=$1 AND "missionCategory"=$2',
            s.uid, category)
        if stored == start:
            continue
        for mission in issued(category, now):
            stage = current_stage(mission, now)
            row = await s.one("Mission", missionMasterId=mission.id_)
            if row:
                await s.update(
                    "Mission", row,
                    currentMissionStageMasterId=stage.id_,
                    missionCurrentCount=0,
                    isCleared=False,
                    isRewardReceived=False,
                )
            else:
                await s.insert(
                    "Mission",
                    missionMasterId=mission.id_,
                    currentMissionStageMasterId=stage.id_,
                    missionCurrentCount=0,
                    isCleared=False,
                    isRewardReceived=False,
                )
        await s.conn.conn.execute(
            f'INSERT INTO {CYCLE_TABLE} ("userId","missionCategory","cycleStartAt") '
            'VALUES ($1,$2,$3) ON CONFLICT ("userId","missionCategory") '
            'DO UPDATE SET "cycleStartAt"=EXCLUDED."cycleStartAt"',
            s.uid, category, start)


def chain_target(category: int, ident: int):
    """The meta mission a cleared mission feeds, or None.

    100300 counts the day's other daily clears and 200600 counts 100300 itself, so the two
    follow the same rule as the beginner missions already in ``mission_progress``.
    """
    if ident == ALL_DAILIES:
        return ALL_DAILIES_WEEK
    if category == DAILY:
        return ALL_DAILIES
    return None


async def spend_stamina(s, amount: int) -> None:
    """スタミナを消費: the daily and weekly counters share one number."""
    if amount <= 0:
        return
    for category in (DAILY, WEEKLY):
        await mission_progress(s, STAMINA[category], amount, create=True)


async def star_acts(s, count: int) -> None:
    if count <= 0:
        return
    await mission_progress(s, STAR_ACTS, count, create=True)


async def market_exchanges(s, count: int = 1) -> None:
    if count <= 0:
        return
    await mission_progress(s, MARKET_EXCHANGES, count, create=True)


async def free_pack(s) -> None:
    """100400: the jewel shop's 毎日無料パック, counted by ``jewel_shop.purchase``.

    The counter lives here with the other mission counters; the route that triggers it is
    the jewel shop's own, because that route also has to hand over the pack's lineup and
    keep it to once a day.
    """
    await mission_progress(s, FREE_PACK, create=True)


# --- market: regular exchange counter --------------------------------------------------


_upstream_market_buy = None


async def _buy_market_frame(conn, user_id: int, frame_number: int):
    """One upstream market purchase, counted for the weekly mission.

    Both exchange endpoints go through this one function, so the count is per successful
    frame and a batch of ten counts ten. ``Mission`` is added to the refreshed types so the
    client sees the new count without waiting for the next data fetch.
    """
    received, refreshed = await _upstream_market_buy(conn, user_id, frame_number)
    await market_exchanges(State(conn, user_id))
    refreshed.add("Mission")
    return received, refreshed


def install(app):
    """Create the cycle marker table and count market exchanges.

    The daily free pack's counter is wired where the pack is handed over, in
    ``jewel_shop.install``.
    """
    from contextlib import asynccontextmanager

    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with previous(app):
            async with app.acquire_db() as conn:
                await ensure_cycle_table(conn)
            yield
    app.router.lifespan_context = lifespan

    global _upstream_market_buy
    _upstream_market_buy = upstream_shops._buy_market_frame
    upstream_shops._buy_market_frame = _buy_market_frame
