"""Destructive ladder checks ONLY in a disposable yumesute_league_checks DB.

演劇リーグ / 演劇リーグ・トリプルキャスト touches PostgreSQL for the first time here: the
class entities, the 99-row generated field, the official reward packages, and the
payout the official server never finished for triple cast window 3036. test_leagues.py
pins the rules; this check shows those rules land in the real schema, that the login
hook pays exactly what master says, and that the ladder's protocol objects encode.

Make the scratch database from the live one first - the check runs on a copy of the save,
mutates that copy and nothing else:

    docker compose exec -T db psql -U yumesute -d postgres -c 'CREATE DATABASE yumesute_league_checks'
    docker compose exec -T db sh -c 'pg_dump -U yumesute -d yumesute | psql -qU yumesute -d yumesute_league_checks'

Then:  uv run --locked python tests/check_leagues.py
Afterwards:  docker compose exec -T db psql -U yumesute -d postgres -c 'DROP DATABASE yumesute_league_checks'
"""
import asyncio
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = "yumesute_league_checks"
ACCOUNT = 5377823715


def force_scratch_database():
    """Point the app's own config at the scratch database before anything connects.

    The name is replaced unconditionally, so this file can never run against a save.
    """
    from helpers.config import config, database

    config["database"]["database"] = SCRATCH
    database.database = SCRATCH


def triples(received):
    """Grant results back to comparable (thing_type, thing_id, quantity) triples."""
    return [(int(item.type), int(item.id_ or 0), int(item.quantity)) for item in received or []]


async def main():
    sys.path[:0] = [str(ROOT), str(ROOT / "tools"), str(ROOT / "vendor/server-of-dreams")]
    import httpx
    from helpers.auth import make_session_jwt
    from helpers.cache import cache
    from protocol import envelope

    force_scratch_database()
    import serve  # the production app, now bound to the scratch database
    import league_schedule as schedule
    import leagues
    import missions
    from gameplay import State
    from helpers.cache import load_master_data
    from models import FinishLiveResult

    app = serve.app
    # The app loads master data inside its lifespan; this run drives the app without one.
    load_master_data()
    schedule.extend_cache()
    await app.yume_setup()
    async with app.acquire_db() as conn:
        await leagues.ensure_field_table(conn)
        # The login hook also reissues the daily/weekly missions, so its marker table has to
        # exist too - this run drives the app without the lifespan that would create it.
        await missions.ensure_cycle_table(conn)
        user = await conn.conn.fetchrow('SELECT "playerRank","playerRankLimit","name" FROM "user" '
                                        'JOIN user_profile USING ("userId") WHERE "userId"=$1', ACCOUNT)
    assert user, f"{SCRATCH} must be a copy of the live save"
    print(f"PASS: running against {SCRATCH} (account rank {user['playerRank']}, "
          f"cap {user['playerRankLimit']})")

    # --- the login payout the official server never finished ---------------------------
    async def balance():
        async with app.acquire_db() as conn:
            items = await conn.conn.fetch('SELECT "itemMasterId",stock FROM item WHERE "userId"=$1', ACCOUNT)
            currency = await conn.conn.fetchrow('SELECT coin,"freeJewel","paidJewel" FROM currency '
                                                'WHERE "userId"=$1', ACCOUNT)
            trophies = await conn.conn.fetch('SELECT "trophyMasterId" FROM trophy WHERE "userId"=$1', ACCOUNT)
        return (Counter({**{(1, r["itemMasterId"]): r["stock"] for r in items},
                         (12, 0): currency["coin"], (13, 0): currency["freeJewel"],
                         (14, 0): currency["paidJewel"]}),
                {r["trophyMasterId"] for r in trophies})

    from progression import refresh_daily

    before, trophies = await balance()
    await refresh_daily(app, ACCOUNT)          # exactly what POST /api/Login runs
    after, owned = await balance()
    paid_window = 3036
    expected = Counter()
    trophies_expected = set()
    for things in (
        leagues._things(leagues.master_reward_package(330)),                     # demotion
        leagues._things(leagues.group_rank_package("triple_cast", 3, 20)),       # group rank 20
        leagues._things(leagues.all_class_package("triple_cast", paid_window, 1702)),  # all-class
    ):
        for kind, ident, amount in things:
            # Balances cover items and currency; trophies are possessions, checked below.
            if kind in (1, 12, 13, 14):
                expected[(kind, ident if kind == 1 else 0)] += amount
            else:
                trophies_expected.add(ident)
    delta = after - before
    assert delta == expected, (dict(sorted(delta.items())), dict(sorted(expected.items())))
    assert trophies_expected and trophies_expected <= (owned - trophies), \
        (trophies_expected, owned - trophies)
    print("PASS: login pays the unpaid window 3036 exactly as master defines it:",
          dict(sorted(expected.items())), "+ trophies", sorted(trophies_expected))

    async with app.acquire_db() as conn:
        row = await conn.conn.fetchrow('SELECT * FROM triple_cast_history WHERE "userId"=$1 AND '
                                       '"tripleCastMasterId"=$2', ACCOUNT, paid_window)
        basic = await conn.conn.fetchrow('SELECT * FROM triple_cast_basic WHERE "userId"=$1', ACCOUNT)
        profile = await conn.conn.fetchrow('SELECT "leagueClass" FROM user_profile WHERE "userId"=$1',
                                           ACCOUNT)
        merged = await leagues.push(State(conn, ACCOUNT), "triple_cast", only_official=True)
    assert row["isSendedReward"] and row["classChangeType"] == 3 and row["historyCount"] == 1, dict(row)
    assert basic["currentClassType"] == 2 and basic["bestClassType"] == 4, dict(basic)
    assert profile["leagueClass"] == 2, dict(profile)
    assert not merged.class_reward_received_things and not merged.group_rank_received_things
    print("PASS: history marked paid, ladder demoted and mirrored into UserProfile; "
          "a second login pays nothing")

    # --- playing a live league window -------------------------------------------------
    live = schedule.live("league")
    assert live, "no league window is live for the scratch run"
    async with app.acquire_db() as conn, conn.transaction():
        state = State(conn, ACCOUNT)
        await leagues.begin(state, "league", live)
        party = next(p for p in await state.rows("Party") if p["name"])
        cards = await leagues.party_cards(state, party["id"])
        # The same entry point Lives/FinishAndValidate uses for a ladder run.
        outcome = FinishLiveResult()
        before_score, best = await leagues.score_league(state, "league", live, cards, 1_500_000, 3,
                                                        party["leaderPosition"], outcome)
        board = await leagues.standings(state, "league", live)
        present = state.present()
    assert (before_score, best) == (0, 1_500_000)
    assert 1 <= board["group_rank"] <= leagues.RIVAL_COUNT + 1
    assert board["group_size"] == leagues.RIVAL_COUNT + 1
    assert triples(outcome.league_rewards) == leagues.play_reward_things(live)
    assert outcome.league_result.after_group_ranking == board["group_rank"]
    assert present, "the ladder entities were not marked for the client"
    print(f"PASS: league window {live.id} (music {live.music}, bonus {live.sense_ids}) played - "
          f"group rank {board['group_rank']} of {board['group_size']}, promotion line "
          f"{board['borders']}")

    async with app.acquire_db() as conn:
        member = await conn.conn.fetchrow('SELECT * FROM league_group_member WHERE "userId"=$1 AND '
                                          '"leagueMasterId"=$2', ACCOUNT, live.id)
        high = await conn.conn.fetchrow('SELECT * FROM league_high_score_party WHERE "userId"=$1 AND '
                                        '"leagueMasterId"=$2', ACCOUNT, live.id)
        slots = await conn.conn.fetch('SELECT * FROM league_high_score_party_slot WHERE "userId"=$1',
                                      ACCOUNT)
        rivals = await conn.conn.fetch('SELECT "classType",count(*) n FROM preservation_league_rival '
                                       'WHERE "userId"=$1 AND kind=$2 GROUP BY 1', ACCOUNT, "league")
        counted = await conn.conn.fetchrow('SELECT "missionCurrentCount" FROM mission WHERE "userId"=$1 '
                                           'AND "missionMasterId"=$2', ACCOUNT, 200100)
    assert member["bestScore"] == 1_500_000 and high["musicMasterId"] == live.music
    assert len(slots) == 5 and all(s["currentStatus"] for s in slots)
    assert len(high["slots"]) == 5, "the party row lost its inline slots"
    assert {r["classType"] for r in rivals} == set(leagues.CLASSES)
    assert sum(r["n"] for r in rivals) == leagues.RIVAL_COUNT * len(leagues.CLASSES)
    assert counted and counted["missionCurrentCount"] >= 1, \
        "the weekly 演劇リーグ mission never counted the run"
    print(f"PASS: score, party (5 inline slots + {len(slots)} slot rows) and "
          f"{sum(r['n'] for r in rivals)} labelled rivals persisted; "
          f"weekly mission 200100 count {counted['missionCurrentCount'] if counted else 0}")

    # --- an ungraded window settles, promotes and pays --------------------------------
    closed = schedule.official("league")[-1]
    async with app.acquire_db() as conn, conn.transaction():
        state = State(conn, ACCOUNT)
        await leagues.begin(state, "league", closed)
        cards = await leagues.party_cards(state, party["id"])
        await leagues.post(state, "league", closed, 2_000_000, cards, 3, party["leaderPosition"])
        outcome = await leagues.settle(state, "league", closed)
        merged = leagues.LeagueReceiveResults()
        promoted = await leagues.pay(state, "league", closed, outcome, merged)
    assert outcome["official"] is False and outcome["change"] == leagues.int_change("Up")
    assert promoted == 3, promoted
    up = leagues._things(leagues.master_reward_package(
        leagues.ladder(closed, 2).up_reward_package_master_id))
    band = leagues._things(leagues.group_rank_package("league", 2, outcome["group_rank"]))
    assert triples(merged.class_reward_received_things) == up
    assert triples(merged.group_rank_received_things) == band
    print(f"PASS: an ungraded window settled on its own ranks - group {outcome['group_rank']} "
          f"promoted Cast, class package {up}, group band {band}")

    async with app.acquire_db() as conn:
        basic = await conn.conn.fetchrow('SELECT * FROM league_basic WHERE "userId"=$1', ACCOUNT)
    assert basic["currentClassType"] == 3 and basic["bestClassType"] == 6, dict(basic)
    print("PASS: league_basic advanced to Cast, best class still Star")

    # --- the protocol side, through the installed routes ------------------------------
    headers = {"Authorization": "Bearer " + make_session_jwt(ACCOUNT, "AppStore")}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://localhost", headers=headers) as client:
        async def call(method, path, content=None):
            response = await client.request(method, path, content=content)
            assert response.status_code == 200, (path, response.status_code)
            return envelope(response.content)[1]  # envelope() raises on any fault

        menu = await call("POST", "/api/Leagues/TopMenuInformation")
        async with app.acquire_db() as conn, conn.transaction():
            fresh = await leagues.standings(State(conn, ACCOUNT), "league", live)
        assert menu[3] == live.music, menu[:6]
        assert menu[6] == fresh["group_rank"] and menu[12] == fresh["group_size"], menu
        assert menu[8] == fresh["borders"]["up_border_score"], menu
        assert menu[9] == fresh["borders"]["up_border_ranking"], menu
        assert menu[13] is True, "the account has joined this window"
        print(f"PASS: TopMenuInformation reports window {live.id} with song {menu[3]}, "
              f"rank {menu[6]} and the promotion line {menu[8]} ({menu[9]}th place)")

        group = await call("POST", f"/api/Leagues/GroupRanking/{live.id}/1")
        names = {row[5] for row in group[:-1]}
        assert len(group) == leagues.RIVAL_COUNT + 1, len(group)
        assert all(name.startswith(leagues.RIVAL_LABEL) for name in names), "unlabelled rival"
        # The last row is this account: its public id, name and the score it just set.
        assert group[-1][1] == str(ACCOUNT) and group[-1][2] == 1_500_000, group[-1][:3]
        assert group[-1][5] == user["name"] and len(group[-1][7]) == 5, group[-1][5]
        global_rows = await call("POST", f"/api/Leagues/GlobalRanking/{live.id}")
        assert len(global_rows) <= leagues.GLOBAL_PAGE + 1
        triple_menu = await call("POST", "/api/TripleCast/TopMenuInformation")
        assert triple_menu[3] == schedule.live("triple_cast").music
        print(f"PASS: group ranking {len(group)} rows, global page {len(global_rows)} rows, "
              f"triple cast menu live - all generated rivals labelled {leagues.RIVAL_LABEL}")

        rewards = await call("GET", "/api/TripleCast")
        assert not rewards[0] and not rewards[3], rewards[:4]
        print("PASS: GET /api/TripleCast answers without paying anything twice")

    # --- the real in-game path: start a league live and finish it ----------------------
    from helpers.msgpack import pack
    from models import (BaseScoreBlock, FinishLivePayload, NoteJudgePayload, SenseScoreBlock,
                        StartLivePayload)
    from models.enums import TimingTypes

    def chained(blocks, fields):
        """Score blocks whose hash chain the server's verifier accepts."""
        running = 0
        for block in blocks:
            running += sum(int(getattr(block, name)) for name in fields)
            block.hash = running
        return blocks

    window = schedule.live("league")
    chart = next(m for m in cache.live_master
                 if m.music_master_id == window.music and int(m.difficulty) == 3)
    base = chained(
        [BaseScoreBlock(score=1200 + 10 * i, life=1, note_id=i,
                        timing_type=TimingTypes.PERFECT, combo=i + 1) for i in range(60)],
        ("score", "life", "note_id", "timing_type", "combo"))
    sense = chained(
        [SenseScoreBlock(score=9000, life=1, time_event_second=10 * i,
                         sense_id=window.sense_ids[0], combo=i + 1) for i in range(5)],
        ("score", "life", "time_event_second", "sense_id", "combo"))
    total = sum(b.score for b in base) + sum(b.score for b in sense)
    payload = FinishLivePayload(
        score=total, max_combo=len(base), is_cleared=True,
        judges=[NoteJudgePayload(timing=TimingTypes.PERFECT, count=len(base))],
        base_score_blocks=base, sense_score_blocks=sense, star_act_score_blocks=[])

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://localhost", headers=headers) as client:
        async def raw(path, body):
            response = await client.post(path, content=body)
            assert response.status_code == 200, (path, response.status_code)
            return response.content

        async with app.acquire_db() as conn:
            stamina_before = await conn.conn.fetchval(
                'SELECT "currentStamina" FROM "user" WHERE "userId"=$1', ACCOUNT)
        started = envelope(await raw("/api/Lives/Start", pack(StartLivePayload(
            party_id=party["id"], live_master_id=chart.id_, league_master_id=window.id,
            use_stamina=True, stamina_consumption_ratio=1, live_setting_master_id=1))))[1]
        assert started and started[0], "no live unit came back"
        async with app.acquire_db() as conn:
            active = await conn.conn.fetchrow('SELECT * FROM active_live WHERE "userId"=$1', ACCOUNT)
            context = await conn.conn.fetchrow(
                'SELECT * FROM preservation_live_context WHERE "userId"=$1', ACCOUNT)
            stamina_after = await conn.conn.fetchval(
                'SELECT "currentStamina" FROM "user" WHERE "userId"=$1', ACCOUNT)
        assert active["liveMasterId"] == chart.id_ and context["mode"] == "league"
        assert context["masterId"] == window.id
        # This window's song is a normal priced song (28 stamina), but a ladder run is
        # free to replay even though the payload asks for stamina: nothing is charged.
        assert active["staminaSpent"] is False, dict(active)
        assert stamina_after == stamina_before, (stamina_before, stamina_after)
        # The run plays against the window's own bonus notation, not the generic eight
        # windows a normal live gets: the sense timings come from SenseNotationMaster.
        notation = next(n for n in cache.sense_notation_master
                        if n.id_ == window.sense_ids[0])
        windows = set(started[1] or {})
        assert windows == {d.timing_second for d in notation.details} | {0}, (
            f"{len(windows)} sense windows instead of the notation's {len(notation.details)} "
            f"plus the opening window: extra {sorted(windows - {d.timing_second for d in notation.details} - {0})}, "
            f"missing {sorted({d.timing_second for d in notation.details} - windows)}")
        print(f"PASS: Lives/Start opened league window {window.id} (chart {chart.id_}, "
              f"{len(started[0])} actors, all {len(notation.details)} sense windows of bonus "
              f"notation {window.sense_ids[0]} plus the opening window, no stamina charged)")

        finished = envelope(await raw("/api/Lives/FinishAndValidate", pack(payload)))[1]
    async with app.acquire_db() as conn:
        member = await conn.conn.fetchrow('SELECT * FROM league_group_member WHERE "userId"=$1 AND '
                                          '"leagueMasterId"=$2', ACCOUNT, window.id)
        high = await conn.conn.fetchrow('SELECT * FROM league_high_score_party WHERE "userId"=$1 AND '
                                        '"leagueMasterId"=$2', ACCOUNT, window.id)
        history = await conn.conn.fetchrow('SELECT * FROM league_history WHERE "userId"=$1 AND '
                                           '"leagueMasterId"=$2', ACCOUNT, window.id)
        rivals_now = await conn.conn.fetchval('SELECT count(*) FROM preservation_league_rival '
                                              'WHERE "userId"=$1 AND kind=$2 AND "masterId"=$3',
                                              ACCOUNT, "league", window.id)
    # An earlier check run already set 1,500,000, so the ladder keeps the better of the two.
    assert member["bestScore"] == max(1_500_000, total), (member["bestScore"], total)
    assert high["highScore"] == max(1_500_000, total), (high["highScore"], total)
    assert history["isPlayed"]
    assert finished[1], "the per-play reward package was missing from the finish result"
    assert finished[25], "the finish result carried no ladder result"
    assert rivals_now == leagues.RIVAL_COUNT * len(leagues.CLASSES), rivals_now
    print(f"PASS: a real league run through Lives/FinishAndValidate posted {total} points "
          f"into window {window.id} and counted the weekly mission")

    # --- the triple cast run: three parties, three bonus notations ----------------------
    from models import StartTripleCastLivePayload

    window = schedule.live("triple_cast")
    chart = next(m for m in cache.live_master
                 if m.music_master_id == window.music and int(m.difficulty) == 3)
    notations = [
        {d.timing_second for d in next(n for n in cache.sense_notation_master if n.id_ == ident).details}
        for ident in window.sense_ids
    ]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://localhost", headers=headers) as client:
        async with app.acquire_db() as conn:
            cast_stamina = await conn.conn.fetchval(
                'SELECT "currentStamina" FROM "user" WHERE "userId"=$1', ACCOUNT)
        started = envelope((await client.post(
            "/api/Lives/StartTripleCastLive",
            content=pack(StartTripleCastLivePayload(m_live_id=chart.id_, m_triple_cast_id=window.id)),
        )).content)[1]
    assert len(started[0]) == 3, f"{len(started[0])} live units"
    for position, unit in enumerate(started[0]):
        assert unit[12] == position + 1 and unit[0] and unit[11], (
            f"order {unit[12]}: {len(unit[0] or {})} actors, active live {unit[11]}")
        # Each order plays against its own bonus set, read through the party-row shim.
        sent = set(unit[1])
        # The opening window at second 0 only appears when a party starts with sense
        # lights, so the notation's own windows are what has to match, order by order.
        assert sent - {0} == notations[position], (
            f"order {position + 1}: extra {sorted(sent - notations[position] - {0})}, "
            f"missing {sorted(notations[position] - sent)}")
    async with app.acquire_db() as conn:
        active = await conn.conn.fetchrow('SELECT * FROM active_live WHERE "userId"=$1', ACCOUNT)
        context = await conn.conn.fetchrow(
            'SELECT * FROM preservation_live_context WHERE "userId"=$1', ACCOUNT)
    assert active["liveMasterId"] == chart.id_ and context["mode"] == "triple_cast"
    assert active["staminaSpent"] is False, dict(active)
    assert context["masterId"] == window.id, dict(context)
    orders = (context["extra"] or {}).get("orders") or {}
    assert sorted(int(k) for k in orders) == [1, 2, 3], orders
    async with app.acquire_db() as conn:
        assert await conn.conn.fetchval('SELECT "currentStamina" FROM "user" WHERE "userId"=$1',
                                        ACCOUNT) == cast_stamina, "a ladder run charged stamina"
    print(f"PASS: Lives/StartTripleCastLive opened window {window.id} (music {window.music}, "
          f"chart {chart.id_}) with 3 units on notations {list(window.sense_ids)}, stamina untouched")

    await app.close()


if __name__ == "__main__":
    asyncio.run(main())
