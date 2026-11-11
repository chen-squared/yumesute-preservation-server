"""Destructive daily/weekly mission checks ONLY in a disposable yumesute_mission_checks DB.

The mission cycles are the part of them that first touches PostgreSQL: the cycle marker
table, the reissued rows, the counters that exist only because a real route ran, and the
reward claim that pays what master says. test_missions.py pins the conditions, the clocks and
the chains against MissionMaster; this check shows those rules land in the real schema, on a
copy of the recovered save, through the same routes the iPad calls.

Make the scratch database from the live one first - the check mutates that copy and nothing
else:

    docker compose exec -T db psql -U yumesute -d postgres -c 'CREATE DATABASE yumesute_mission_checks'
    docker compose exec -T db sh -c 'pg_dump -U yumesute -d yumesute | psql -qU yumesute -d yumesute_mission_checks'

Then:  uv run --locked python tests/check_missions.py
Afterwards:  docker compose exec -T db psql -U yumesute -d postgres -c 'DROP DATABASE yumesute_mission_checks'

The clone has to carry the save's stamina (one live pays over 300 to reach the daily goal),
its parties and lesson state, and a market frame it can afford. Recreate the clone before
rerunning. Never point it at a save you intend to keep.
"""
import asyncio
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = "yumesute_mission_checks"
ACCOUNT = 5377823715
MICRO = 1_000_000
DAY = 86_400 * MICRO
JST = timezone(timedelta(hours=9))
MISSION_UNION_KEY = 48


def force_scratch_database():
    """Point the app's own config at the scratch database before anything connects.

    The name is replaced unconditionally, so this file can never run against a save.
    """
    from helpers.config import config, database

    config["database"]["database"] = SCRATCH
    database.database = SCRATCH


def first_stage(mission_id):
    from helpers.cache import cache

    m = next(x for x in cache.mission_master if x.id_ == mission_id)
    return sorted(m.stages or [], key=lambda x: x.mission_stage_order)[0]


def goal(mission_id):
    return int(first_stage(mission_id).stage_goal_value)


def stage_things(mission_id):
    return [(int(r.thing_type), r.thing_id, int(r.thing_quantity)) for r in first_stage(mission_id).rewards or []]


def balance_key(thing_type, thing_id):
    """A balance counter key for one thing: items are keyed by id, currencies are not."""
    return (int(thing_type), int(thing_id or 0) if int(thing_type) == 1 else 0)


async def main():
    sys.path[:0] = [str(ROOT), str(ROOT / "tools"), str(ROOT / "vendor/server-of-dreams")]
    import httpx
    from helpers.auth import make_session_jwt
    from helpers.cache import cache, load_master_data
    from helpers.msgpack import pack
    from helpers.shops import market_frame
    from protocol import envelope, expand, unpack_stream

    force_scratch_database()
    import serve  # the production app, now bound to the scratch database
    import jewel_shop
    import missions
    from gameplay import State
    from progression import refresh_daily
    from models import (BaseScoreBlock, FinishLivePayload, NoteJudgePayload, PurchaseItemPayload,
                        SenseScoreBlock, StarActScoreBlock, StartLessonPayload, StartLivePayload)
    from models.enums import TimingTypes

    app = serve.app
    # The app loads master data inside its lifespan; this run drives the app without one.
    load_master_data()
    await app.yume_setup()
    async with app.acquire_db() as conn:
        await missions.ensure_cycle_table(conn)
        stamina = await conn.conn.fetchval('SELECT "currentStamina" FROM "user" WHERE "userId"=$1',
                                           ACCOUNT)
    assert stamina is not None, f"{SCRATCH} must be a copy of the live save"
    print(f"PASS: running against {SCRATCH} (stamina {stamina})")

    async def rows():
        async with app.acquire_db() as conn:
            found = await conn.conn.fetch(
                'SELECT "missionMasterId","currentMissionStageMasterId","missionCurrentCount",'
                '"isCleared","isRewardReceived" FROM mission WHERE "userId"=$1 '
                'AND "missionMasterId">=100000 AND "missionMasterId"<300000', ACCOUNT)
        return {r["missionMasterId"]: (r["missionCurrentCount"], r["isCleared"],
                                       r["isRewardReceived"], r["currentMissionStageMasterId"])
                for r in found}

    async def balances():
        async with app.acquire_db() as conn:
            items = await conn.conn.fetch('SELECT "itemMasterId",stock FROM item WHERE "userId"=$1',
                                          ACCOUNT)
            currency = await conn.conn.fetchrow('SELECT coin,"freeJewel","paidJewel" FROM currency '
                                                'WHERE "userId"=$1', ACCOUNT)
        return Counter({**{(1, r["itemMasterId"]): r["stock"] for r in items},
                        (12, 0): currency["coin"], (13, 0): currency["freeJewel"],
                        (14, 0): currency["paidJewel"]})

    now = time.time_ns() // 1000
    saved = await rows()
    assert saved, "the clone has no daily/weekly mission rows at all"
    print(f"PASS: before the login hook the save carries {len(saved)} daily/weekly rows - "
          f"100200 count {saved[100200][0]}, cleared {saved[100200][1]}, "
          f"claimed {saved[100200][2]}")

    # --- the login hook reissues both cycles -------------------------------------------
    await refresh_daily(app, ACCOUNT)            # exactly what POST /api/Login runs
    issued = {m.id_ for m in missions.issued(missions.DAILY, now)}
    issued |= {m.id_ for m in missions.issued(missions.WEEKLY, now)}
    current = await rows()
    assert issued <= set(current), sorted(issued - set(current))
    stale = {i: current[i] for i in sorted(issued) if current[i][:3] != (0, False, False)}
    assert not stale, stale
    # 100100 closed on 2023-10-11, so it is left exactly as the recovered save carried it.
    assert current.get(100100, (0, False, False))[:3] == (0, False, False), current.get(100100)
    async with app.acquire_db() as conn:
        markers = await conn.conn.fetch('SELECT "missionCategory","cycleStartAt" FROM '
                                        'preservation_mission_cycle WHERE "userId"=$1', ACCOUNT)
    starts = {r["missionCategory"]: r["cycleStartAt"] for r in markers}
    assert starts == {missions.DAILY: missions.cycle_start(missions.DAILY, now),
                      missions.WEEKLY: missions.cycle_start(missions.WEEKLY, now)}, starts
    print(f"PASS: the login hook reissued {len(issued)} missions - daily cycle "
          f"{datetime.fromtimestamp(starts[missions.DAILY] / MICRO, JST):%Y-%m-%d %H:%M}, weekly "
          f"{datetime.fromtimestamp(starts[missions.WEEKLY] / MICRO, JST):%Y-%m-%d %H:%M} JST")

    async with app.acquire_db() as conn, conn.transaction():
        await missions.rotate(State(conn, ACCOUNT), now + 60 * MICRO)
    assert await rows() == current, "a second login inside the same cycle changed mission rows"
    print("PASS: a second login inside the same cycle changes nothing")

    # --- a real live: the stamina it paid and the star acts it triggered ---------------
    async with app.acquire_db() as conn:
        owned = [r["musicMasterId"] for r in await conn.conn.fetch(
            'SELECT "musicMasterId" FROM music WHERE "userId"=$1 AND "isPossession"', ACCOUNT)]
        async with conn.transaction():
            party = next(p for p in await State(conn, ACCOUNT).rows("Party") if p["name"])
    priced = [m for m in cache.music_master if m.id_ in owned and int(m.stamina_consumption) >= 20]
    assert priced, "the clone owns no song that costs stamina"
    song = min(priced, key=lambda m: m.stamina_consumption)
    cost = int(song.stamina_consumption)
    ratio = max(1, -(-goal(100000) // cost))
    # One live has to pay the whole daily goal, and the save only carries its recovered
    # stamina. The clone is topped up - this writes to yumesute_mission_checks and nowhere
    # else; the ratio itself is the game's own multi-consumption field.
    needed = cost * ratio + 100
    if stamina < needed:
        from helpers.stamina import max_stamina, restored_at

        async with app.acquire_db() as conn:
            rank = await conn.conn.fetchval('SELECT "playerRank" FROM "user" WHERE "userId"=$1',
                                            ACCOUNT)
            # ``currentStamina`` alone is not the game's invariant: the effective value is
            # recomputed from maxStaminaRestoredAt, so a top-up has to carry that too.
            at = int(time.time() * 1_000_000)
            await conn.conn.execute(
                'UPDATE "user" SET "currentStamina"=$2,"maxStaminaRestoredAt"=$3 WHERE "userId"=$1',
                ACCOUNT, needed, restored_at(needed, max_stamina(rank), at))
        print(f"PASS: topped the clone up to {needed} stamina of {max_stamina(rank)} (the daily "
              f"goal needs {cost}x{ratio}); the live save is untouched")
        stamina = needed
    assert stamina >= cost * ratio, f"the clone needs {cost * ratio} stamina to reach the daily goal"
    chart = next(c for c in cache.live_master if c.music_master_id == song.id_
                 and int(c.difficulty) == 3)

    def chained(blocks, fields):
        """Score blocks whose hash chain the server's verifier accepts."""
        running = 0
        for block in blocks:
            running += sum(int(getattr(block, name)) for name in fields)
            block.hash = running
        return blocks

    base = chained(
        [BaseScoreBlock(score=1200 + 10 * i, life=1, note_id=i,
                        timing_type=TimingTypes.PERFECT, combo=i + 1) for i in range(60)],
        ("score", "life", "note_id", "timing_type", "combo"))
    sense = chained(
        [SenseScoreBlock(score=9000, life=1, time_event_second=10 * i, sense_id=1, combo=i + 1)
         for i in range(5)],
        ("score", "life", "time_event_second", "sense_id", "combo"))
    acts = chained(
        [StarActScoreBlock(score=50000 + 100 * i, life=1, time_event_second=30 * i, combo=i + 1)
         for i in range(3)],
        ("score", "life", "time_event_second", "combo"))
    payload = FinishLivePayload(
        score=sum(b.score for b in base) + sum(b.score for b in sense) + sum(b.score for b in acts),
        max_combo=len(base), is_cleared=True,
        judges=[NoteJudgePayload(timing=TimingTypes.PERFECT, count=len(base))],
        base_score_blocks=base, sense_score_blocks=sense, star_act_score_blocks=acts)

    headers = {"Authorization": "Bearer " + make_session_jwt(ACCOUNT, "AppStore")}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://localhost", headers=headers) as client:
        async def raw(path, body=b""):
            response = await client.post(path, content=body)
            assert response.status_code == 200, (path, response.status_code)
            return response.content

        def faults(body):
            """The fault array of a response - the part ``envelope`` refuses to return."""
            return [expand(v) for v in unpack_stream(body)][0]

        started = envelope(await raw("/api/Lives/Start", pack(StartLivePayload(
            party_id=party["id"], live_master_id=chart.id_, use_stamina=True,
            stamina_consumption_ratio=ratio, live_setting_master_id=1))))[1]
        assert started and started[0], "no live unit came back"
        async with app.acquire_db() as conn:
            after_start = await conn.conn.fetchval('SELECT "currentStamina" FROM "user" '
                                                   'WHERE "userId"=$1', ACCOUNT)
            active = await conn.conn.fetchrow('SELECT * FROM active_live WHERE "userId"=$1', ACCOUNT)
        assert active["staminaSpent"] is True, dict(active)
        assert after_start == stamina - cost * ratio, (stamina, after_start, cost, ratio)
        envelope(await raw("/api/Lives/FinishAndValidate", pack(payload)))
        current = await rows()
        assert current[100000][:2] == (cost * ratio, cost * ratio >= goal(100000)), current[100000]
        assert current[200000][:2] == (cost * ratio, False), current[200000]
        assert current[200200][:2] == (len(acts), len(acts) >= goal(200200)), current[200200]
        print(f"PASS: a normal live paid {cost}x{ratio} stamina and both stamina missions moved to "
              f"{cost * ratio}; the 3 star acts counted once each for 200200")

        # --- a real lesson: 100200 稽古を1回 -------------------------------------------
        async with app.acquire_db() as conn:
            lesson_base = (await conn.conn.fetch(
                'SELECT "characterBaseMasterId" FROM character_base WHERE "userId"=$1 '
                'ORDER BY "characterBaseMasterId" LIMIT 1', ACCOUNT))[0]["characterBaseMasterId"]
        lesson_chart = next(c for c in cache.live_master if c.music_master_id in owned)
        envelope(await raw("/api/Lives/StartLesson", pack(StartLessonPayload(
            character_base_master_id=lesson_base, live_master_id=lesson_chart.id_))))
        envelope(await raw("/api/Lives/FinishAndValidate", pack(payload)))
        current = await rows()
        assert current[100200][:2] == (1, True), current[100200]
        print(f"PASS: one lesson cleared 100200 (稽古を1回) for base {lesson_base}")

        # --- the regular market: 200500 counts exchanges, not items --------------------
        envelope(await raw("/api/Shops/GetOrRefreshMarket"))
        async with app.acquire_db() as conn:
            frame = await conn.conn.fetchrow(
                'SELECT "frameNumber","marketFrameThingMasterId","discountPercent" FROM market_thing '
                'WHERE "userId"=$1 AND NOT "hasPurchased" ORDER BY "frameNumber" LIMIT 1', ACCOUNT)
        spec = market_frame(frame["marketFrameThingMasterId"])
        price = int(spec.required_thing_quantity or 0)
        if frame["discountPercent"]:
            price = price * (100 - int(frame["discountPercent"])) // 100
        paid_before = await balances()
        bought = envelope(await raw(f"/api/Shops/ExchangeMarketThing/{frame['frameNumber']}"))
        paid_after = await balances()
        key = balance_key(spec.required_thing_type, spec.required_thing_id)
        current = await rows()
        assert paid_after[key] == paid_before[key] - price, (key, price, paid_before[key],
                                                             paid_after[key])
        assert current[200500][:2] == (1, False), current[200500]
        assert any(entry[0] == MISSION_UNION_KEY for entry in bought[2]), \
            "the exchange response did not carry the fresh mission rows"
        print(f"PASS: market frame {frame['frameNumber']} paid {price} of thing {key} and counted "
              f"once for 200500, mission rows included in the response")

        # --- the jewel shop's daily free pack: 100400, and its once-a-day cap ----------
        async def shop_row(item_id):
            async with app.acquire_db() as conn:
                return await conn.conn.fetchrow(
                    'SELECT "purchaseCount","totalPurchaseCount","rePurchaseDate" FROM jewel_shop '
                    'WHERE "userId"=$1 AND "jewelShopItemMasterId"=$2', ACCOUNT, item_id)

        async def stamina_of():
            async with app.acquire_db() as conn:
                return await conn.conn.fetchval('SELECT "currentStamina" FROM "user" '
                                                'WHERE "userId"=$1', ACCOUNT)

        recovered = await shop_row(4101)
        stamp = recovered["rePurchaseDate"]
        print(f"PASS: the save carries the official shop history for 4101 - "
              f"{recovered['purchaseCount']} in the period, {recovered['totalPurchaseCount']} ever, "
              f"open again at {datetime.fromtimestamp(stamp / MICRO, timezone.utc):%Y-%m-%d %H:%M} "
              f"in the master's own wall-clock hours, which is the mission cycle's 05:00 boundary")

        coin_before = (await balances())[(12, 0)]
        stamina_before = await stamina_of()
        free = envelope(await raw("/api/Shops/Purchase",
                                  pack(PurchaseItemPayload(m_jewel_shop_item_id=4101))))
        after = await balances()
        current = await rows()
        assert current[100400][:2] == (1, True), current[100400]
        assert any(entry[0] == MISSION_UNION_KEY for entry in free[2]), \
            "the free pack response did not carry the fresh mission rows"
        stamped = await shop_row(4101)
        assert after[(12, 0)] == coin_before + 10_000 and await stamina_of() == stamina_before + 60, \
            (coin_before, after[(12, 0)], stamina_before, await stamina_of())
        assert stamped["purchaseCount"] == 1 and stamped["totalPurchaseCount"] == \
            recovered["totalPurchaseCount"] + 1, dict(stamped)
        assert stamped["rePurchaseDate"] == jewel_shop.period_end(1, time.time_ns() // 1000), \
            (stamped["rePurchaseDate"], datetime.fromtimestamp(stamped["rePurchaseDate"] / MICRO, JST))
        print("PASS: 毎日無料パック handed over its lineup (スタミナ60 + コイン10000), cleared 100400, "
              f"and stamped itself open again at "
              f"{datetime.fromtimestamp(stamped['rePurchaseDate'] / MICRO, JST):%Y-%m-%d %H:%M} JST")

        refused = faults(await raw(
            "/api/Shops/Purchase", pack(PurchaseItemPayload(m_jewel_shop_item_id=4101))))
        again_after = await balances()
        current = await rows()
        assert len(refused) == 1 and "PurchaseLimitExceeded" in str(refused[0]), refused
        assert await stamina_of() == stamina_before + 60 and again_after[(12, 0)] == after[(12, 0)], \
            "the second tap the same day still paid out"
        assert current[100400][:2] == (1, True) and current[100300][0] == 3, current
        once = await shop_row(4101)
        assert once["purchaseCount"] == 1 and once["totalPurchaseCount"] == \
            stamped["totalPurchaseCount"], dict(once)
        stamina_after_pack = await stamina_of()
        lifetime = await shop_row(4301)
        refused = faults(await raw(
            "/api/Shops/Purchase", pack(PurchaseItemPayload(m_jewel_shop_item_id=4301))))
        assert len(refused) == 1 and "PurchaseLimitExceeded" in str(refused[0]), refused
        assert tuple(await shop_row(4301)) == tuple(lifetime), "a lifetime pack was claimed twice"
        print(f"PASS: the same day's second tap and the already-claimed one-time pack 4301 "
              f"({lifetime['totalPurchaseCount']} ever) were both refused and paid nothing")

    # --- 100300 and 200600: missions that count other missions -------------------------
    current = await rows()
    assert current[missions.ALL_DAILIES][:2] == (3, True), current[missions.ALL_DAILIES]
    assert current[missions.ALL_DAILIES_WEEK][:2] == (1, False), current[missions.ALL_DAILIES_WEEK]
    print("PASS: the three daily clears chained into 100300 (goal 3) and once into 200600")

    # --- receiving the day's rewards ---------------------------------------------------
    expected = Counter()
    for ident in (100000, 100200, 100300, 100400):
        for thing in stage_things(ident):
            expected[balance_key(thing[0], thing[1])] += thing[2]
    was = await balances()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://localhost", headers=headers) as client:
        envelope((await client.post("/api/Missions/receiveRewards?missionCategory=4")).content)
    delta = await balances() - was
    assert delta == expected, (dict(sorted(delta.items())), dict(sorted(expected.items())))
    current = await rows()
    assert [current[i][2] for i in (100000, 100200, 100300, 100400)] == [True] * 4, current
    assert [current[i][2] for i in (200000, 200100, 200200, 200300, 200500)] == [False] * 5
    print("PASS: receiving the daily category paid the master's rewards exactly:",
          dict(sorted((k, v) for k, v in delta.items() if v)))

    # --- the next day, then the next week ----------------------------------------------
    week = {i: current[i][0] for i in (200000, 200200, 200500)}
    async with app.acquire_db() as conn, conn.transaction():
        await missions.rotate(State(conn, ACCOUNT), now + DAY)
    current = await rows()
    assert [current[i][:3] for i in (100000, 100200, 100300, 100400)] == [(0, False, False)] * 4, \
        {i: current[i] for i in (100000, 100200, 100300, 100400)}
    assert {i: current[i][0] for i in week} == week, {i: current[i] for i in week}
    print("PASS: the next day's login reset the dailies and left that week's counters alone "
          f"(weekly still {week})")

    # The shop's daily cap rolls on that same 05:00 boundary, so the pack is open again for
    # the reissued 100400 - and its period count restarts instead of climbing. The stamp is
    # pushed back by the same DAY the mission cycle was advanced by, because the route reads
    # the real clock while ``rotate`` was handed the next day.
    async with app.acquire_db() as conn:
        await conn.conn.execute('UPDATE jewel_shop SET "rePurchaseDate" = "rePurchaseDate" - $2 '
                                'WHERE "userId"=$1 AND "jewelShopItemMasterId"=4101', ACCOUNT, DAY)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://localhost", headers=headers) as client:
        envelope((await client.post("/api/Shops/Purchase",
                                    content=pack(PurchaseItemPayload(m_jewel_shop_item_id=4101)))).content)
    rolled = await shop_row(4101)
    current = await rows()
    assert current[100400][:2] == (1, True) and current[100300][:2] == (1, False), current
    assert rolled["purchaseCount"] == 1 and \
        rolled["totalPurchaseCount"] == once["totalPurchaseCount"] + 1, dict(rolled)
    assert await stamina_of() == stamina_after_pack + 60, await stamina_of()
    print("PASS: with the day rolled the pack was open again - period count back to 1, "
          f"{rolled['totalPurchaseCount']} ever, スタミナ60 + コイン10000 again - and cleared the "
          "reissued 100400")

    monday = datetime.fromtimestamp(missions.cycle_start(missions.WEEKLY, now) / MICRO, JST)
    next_week = int((monday + timedelta(days=7, hours=1)).timestamp() * MICRO)
    async with app.acquire_db() as conn, conn.transaction():
        await missions.rotate(State(conn, ACCOUNT), next_week)
    current = await rows()
    weekly = (200000, 200100, 200200, 200300, 200400, 200500, 200600)
    assert [current[i][:3] for i in weekly] == [(0, False, False)] * 7, \
        {i: current[i] for i in weekly}
    print("PASS: Monday's login reset the weekly set too - and 200400 (photo levels) is still 0, "
          "because photo enhancement is a stub upstream and nothing invents its progress")


if __name__ == "__main__":
    asyncio.run(main())
