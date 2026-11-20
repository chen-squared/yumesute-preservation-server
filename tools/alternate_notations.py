"""やねうら (April-Fool) alternate charts: keep the mode open, and record what is played on it.

The archive ships this whole mode. ``AnotherNotationMaster`` holds 112 charts - 30 from the
2024 edition, 33 from 2025, 37 from the 2026 ヤネウラ edition and 12 added on 2026-07-26 - all
of them with ``end_date`` 2100, and all 112 ``Notations/20xxx`` chart folders are here locally
(missing files: 0). What hid them was one date: ``EventMaster`` 21018 (``event_type`` 23 =
``EventTypes.AnotherNotation``, ``is_aside_live_button`` with ``aside_live_button_type`` 3, the
side button on the live screen) closed on 2026-04-08, and the master-date repair only extends
windows that were cut off by the service boundary, so that one was left alone.
``preservation_dates`` holds it open now, the same way the ladder and weekly-course windows
are held. That is the only official row this mode needs: the archived 2024/2025 event rows
(21003, 21010) carry no button, and no banner, top-menu or mission row refers to 21018.

What was missing is the server half of a run. ``Lives/FinishAnotherNotationLive`` is a stub in
the pinned vendor - it returns an empty ``FinishLiveResult`` and writes nothing - so a meme
chart could be played but its result never reached ``another_notation``, the table this
account already has 27 rows of (union key 145, ``AnotherNotation``).

The device also settles the run on its own: measured over nine alternate-chart plays on the
iPad (``logs/backend.log``), each one posts ``Lives/FinishAnotherNotationLive`` and nothing
else - no ``Lives/Start``, no ``Lives/Retire``. The mode opens from the live screen's side
button, plays an archived chart file, and reports the result without ever opening a live
session, so it spends no stamina and there is no ``active_live`` row to finish. That is why
this route cannot borrow the normal finish's "a live must be in progress" precondition: doing
so answers every run with ``InvalidLiveState`` and the client's result panel settles at
0.0000%. A run is instead graded from the score blocks it carries, and where a live *is* in
progress (a normal ``Lives/Start`` that is settled here) its song still has to match the chart.

Official part, straight from the archive and the protocol:

  * the payload carries the same four score-block lists as a normal live and the same hash
    chain, so the run is graded by the same rules as ``live_finish``: ``clear_lamp`` for the
    lamps, ``achievement_rate`` for the percent, ``rate_grade`` for the grade, best-of kept per
    chart, and an auto-play run never banks a record;
  * the record row is exactly the archived shape - ``anotherNotationMasterId``, ``clearLamp``,
    ``rateGrade`` and ``achievementRatePercentRecord`` as a four-decimal string (``101.0000``)
    - and it is pushed back to the client as a ``AnotherNotation`` present object;
  * the chart is played on the ``LiveMaster`` row of its own song and difficulty. All 1490 live
    rows satisfy ``id == music_master_id * 100 + difficulty`` and every one of the 112 alternate
    rows has such a row, which is also how the client picks it
    (``CourseModeModel.SetAnotherNotationLiveMusicMasterId`` matches on the music);
    ``chart_for`` is that rule and the course code uses it too.

Not official - and deliberately not done - is folding an alternate run into the normal per-song
record. The ``live`` row, the live rate, the player total rate, the rank XP a spent-stamina run
would earn, drops, mission counters and song unlocks are all left untouched: these are different
charts with different levels (``Get to Act Life♡`` lv 107 against live row 7404's lv 28), so
writing the song's own record from a meme run would hand out lamps and rate for a chart that was
never played. Nothing is charged on this route either - the client opens the mode without a
live session, so there is no stamina to refund and no cost to invent. The hidden trophy 400017
ヤネウラの名探偵 is not granted either - the master gives no rule for it, only the text
「エイプリルフール【2026】に参加しよう」, and inventing one would be local content.

Which route a run settles on follows ``preservation_live_context``, and ``decide`` is that rule:
a course stage owns its own shared life pool, stage sequence and (for the weekly courses)
ranking row, so a course run reported here is handed to the ``Lives/FinishAndValidate`` body with
the blocks this payload carries - that is what makes the archived ヤネウラ狂奏曲 details playable
either way. A ladder, concert, audition or lesson run is refused on this route instead, so its
accounting cannot be skipped. Everything else is the free meme run above.
``test_alternate_notations.py`` pins the record rules, the route rule and the song-row rule;
``check_alternate_notations.py`` drives both routes over a clone of the save.
"""
import json
import time
from pathlib import Path

from fastapi import APIRouter, Request
from db.user import delete_active_lives
from gameplay import Rejected, master, transaction
from helpers.cache import cache
from helpers.live_result import achievement_rate, clear_lamp, play_totals, rate_grade
from helpers.msgpack import fault, read_request, respond
from helpers.score import verify_score_blocks
from helpers.user_data import current_user_id
from models import (FinishAnotherNotationLivePayload, FinishLivePayload, FinishLiveResult,
                    RateResult, RateUpdateResult)
from models.enums import AchievementRateGrades, ClearLamps
import play_history

router = APIRouter()


def notation(ident):
    """The alternate chart row (``AnotherNotationMaster``), or None."""
    return master('another_notation_master', int(ident or 0))


def chart_for(notation_row):
    """The ``LiveMaster`` row an alternate chart is played on: its own song, its own difficulty.

    Measured on the archive rather than assumed: every live row follows
    ``id == music_master_id * 100 + difficulty`` (1490/1490) and all 112 alternate rows have
    such a row. The levels differ, which is the point of the mode - alternate 65 (Get to Act
    Life♡) is difficulty 4 level 107, live row 7404 is difficulty 4 level 28.
    """
    if notation_row is None:
        return None
    return next(
        (row for row in cache.live_master
         if int(row.music_master_id) == int(notation_row.music_master_id)
         and int(row.difficulty) == int(notation_row.difficulty)),
        None,
    )


def as_live_payload(payload):
    """The same run in the shape ``Lives/FinishAndValidate`` reads.

    Neither route takes the headline figures from the client: the finish recomputes score,
    clear and rate from the blocks, so carrying the four block lists over is enough for a run
    that belongs to another mode to settle there unchanged.
    """
    return FinishLivePayload(
        base_score_blocks=payload.base_score_blocks,
        sense_score_blocks=payload.sense_score_blocks,
        star_act_score_blocks=payload.star_act_score_blocks,
        multi_live_additional_score_blocks=payload.multi_live_additional_score_blocks,
    )


async def live_mode(request) -> str:
    """What kind of live this account is in: ``normal``, a mode name, or ``missing``."""
    uid = current_user_id(request)
    if uid is None:
        return 'missing'
    async with request.app.acquire_db() as conn:
        if not await conn.conn.fetchrow('SELECT 1 FROM active_live WHERE "userId"=$1', uid):
            return 'missing'
        row = await conn.conn.fetchrow(
            'SELECT mode FROM preservation_live_context WHERE "userId"=$1', uid)
    return row['mode'] if row else 'normal'


# What this route does with a run, by the mode its live belongs to.
RECORD = 'record'      # the meme run itself: free play, no live session to close
HANDOFF = 'handoff'    # a 課題曲 stage: the course body owns the life pool and the ranking row
REFUSE = 'refuse'      # another mode's run: its accounting lives on Lives/FinishAndValidate


def decide(mode: str) -> str:
    """Where a run reported on this route is settled (see the module docstring for the why)."""
    if mode == 'course':
        return HANDOFF
    if mode in ('normal', 'missing'):
        return RECORD
    return REFUSE


BASE_FIELDS = ("score", "life", "note_id", "timing_type", "combo")
PROBE = Path(__file__).resolve().parents[1] / 'logs' / 'alt-notation-probe.jsonl'


def _mismatch(blocks, fields):
    """The first block whose hash chain does not reproduce, or None if the chain holds."""
    running = 0
    for index, block in enumerate(blocks or []):
        running += sum(int(getattr(block, name)) for name in fields)
        if int(block.hash) != running:
            return {"index": index, "expected": running, "reported": int(block.hash),
                    "values": {name: int(getattr(block, name)) for name in fields}}
    return None


def probe(request, raw, payload, decision, extra=None):
    """One line in ``logs/alt-notation-probe.jsonl`` per alternate-chart finish.

    The pinned vendor leaves this route a stub, so there is no archived capture of what the
    client really puts on it: the first device runs are the evidence. Each line records the
    decoded top-level shape, the block counts and one sample block, the first hash-chain
    mismatch if there is one, and which decision the route took - enough to pin the protocol
    down without storing a play-by-play.
    """
    line = {"at": int(time.time()), "user": current_user_id(request), "decision": decision}
    if isinstance(raw, list):
        line["wire"] = [f"list[{len(v)}]" if isinstance(v, list) else type(v).__name__
                        for v in raw]
    elif isinstance(raw, dict):
        line["wire"] = f"dict:{sorted(map(str, raw))}"
    else:
        line["wire"] = type(raw).__name__
    if payload is not None:
        blocks = payload.base_score_blocks
        line.update({
            "notation": int(payload.another_notation_master_id),
            "auto": bool(payload.is_auto_play),
            "base": len(blocks or []), "sense": len(payload.sense_score_blocks or []),
            "star": len(payload.star_act_score_blocks or []),
            "multi": len(payload.multi_live_additional_score_blocks or []),
        })
        if blocks:
            first, last = blocks[0], blocks[-1]
            line["first"] = {name: int(getattr(first, name)) for name in BASE_FIELDS}
            line["first"]["hash"] = int(first.hash)
            line["last"] = {name: int(getattr(last, name)) for name in BASE_FIELDS}
            line["last"]["hash"] = int(last.hash)
        for name, attribute, fields in (
                ("base", "base_score_blocks", BASE_FIELDS),
                ("sense", "sense_score_blocks",
                 ("score", "life", "time_event_second", "sense_id", "combo")),
                ("star", "star_act_score_blocks",
                 ("score", "life", "time_event_second", "combo")),
                ("multi", "multi_live_additional_score_blocks",
                 ("score", "life", "time_event_second", "combo"))):
            found = _mismatch(getattr(payload, attribute, None), fields)
            if found:
                line[f"mismatch_{name}"] = found
    if extra:
        line.update(extra)
    try:
        PROBE.parent.mkdir(exist_ok=True)
        with PROBE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass          # a diagnostic never gets to break a live
    return line


@router.post('/api/Lives/FinishAnotherNotationLive', name='Lives_FinishAnotherNotationLive')
async def finish_another_notation(request: Request):
    raw = await read_request(request)
    payload = await read_request(request, FinishAnotherNotationLivePayload)
    if payload is None:
        probe(request, raw, None, "rejected: no payload decoded")
        return respond(FinishLiveResult())
    if not verify_score_blocks(payload):
        # the same tamper check the normal finish runs, on the same block lists
        probe(request, raw, payload, "rejected: score-block hash chain")
        return respond(FinishLiveResult(), faults=[fault('InvalidScoreBlockHash')])
    chart = notation(payload.another_notation_master_id)
    if chart is None:
        probe(request, raw, payload, "rejected: unknown alternate chart")
        return respond(FinishLiveResult(),
                       faults=[fault('InvalidLiveState', 'Unknown alternate chart')])
    mode = await live_mode(request)
    where = decide(mode)
    if where == HANDOFF:
        # 課題曲 run: its shared life pool and ranking row are settled by the course code,
        # which hangs off the normal finish route.
        probe(request, raw, payload, "settled by Lives/FinishAndValidate (course stage)")
        import live_modes
        return await live_modes.settle(request, as_live_payload(payload),
                                       another_notation_master_id=chart.id_)
    if where == REFUSE:
        probe(request, raw, payload, f"rejected: live mode is {mode}")
        return respond(FinishLiveResult(), faults=[fault(
            'InvalidLiveState', 'This live settles through Lives/FinishAndValidate')])

    _, is_cleared = play_totals(payload)
    try:
        async with transaction(request) as s:
            context = await s.conn.conn.fetchrow(
                'SELECT mode FROM preservation_live_context WHERE "userId"=$1', s.uid)
            if context and context['mode'] not in ('normal', 'missing'):
                raise Rejected('Live mode changed before settlement')
            active = await s.conn.conn.fetchrow(
                'SELECT * FROM active_live WHERE "userId"=$1', s.uid)
            live = chart_for(chart)
            if active and (live is None or int(active['liveMasterId']) != int(live.id_)):
                # A run started on another song cannot write this alternate chart's record.
                raise Rejected('Alternate chart is not this live')
            row = await s.one('AnotherNotation', anotherNotationMasterId=int(chart.id_))
            values, run = record_update(row, payload, is_cleared)
            if row:
                if any(row.get(key) != value for key, value in values.items()):
                    await s.update('AnotherNotation', row, **values)
            else:
                await s.insert('AnotherNotation', anotherNotationMasterId=int(chart.id_),
                               **values)
            if active:
                # only a run that opened a live session has one to close
                await s.conn.execute(delete_active_lives(s.uid))
                await s.conn.conn.execute(
                    'DELETE FROM preservation_live_context WHERE "userId"=$1', s.uid)
            await play_history.record(s, live.id_ if live else 0, 'alternate', play_history.summarize(payload),
                                      auto=payload.is_auto_play or bool(active and active['isAutoPlay']),
                                      alternate=chart.id_)
            total_rate = float(await s.conn.conn.fetchval(
                'SELECT "playerRate" FROM user_profile WHERE "userId"=$1', s.uid) or 0.0)
            result = FinishLiveResult(
                clear_lamp=ClearLamps(run['lamp']),
                before_clear_lamp=ClearLamps(run['prev_lamp']),
                rate_grade=AchievementRateGrades(rate_grade(run['rate'])),
                is_high_score=run['high'],
                rate_result=RateResult(
                    achievement_rate_result=RateUpdateResult(
                        best_ever=round(run['prev_rate'], 4), this_time=round(run['rate'], 4)),
                    # an alternate run is not part of the live-rate total, so it reports the
                    # player's rate unchanged rather than pretending to move it
                    total_rate_before=total_rate,
                    total_rate_after=total_rate,
                ),
            )
            probe(request, raw, payload, f"recorded (mode {mode})",
                  {"rate": round(run['rate'], 4), "lamp": run['lamp'],
                   "grade": rate_grade(run['rate']),
                   "best": values['achievementRatePercentRecord'], "high": run['high'],
                   "session": bool(active)})
            return respond(result, present=s.present())
    except Rejected as e:
        probe(request, raw, payload, f"rejected: {e}")
        return respond(FinishLiveResult(), faults=[fault('InvalidLiveState', str(e))])


def record_update(previous, payload, is_cleared):
    """What this run writes into the record row, and its own figures.

    ``previous`` is the account's row for this chart, or None. The rule is the one a normal
    live follows (``live_finish``): the run is graded from its own score blocks, a clear is
    needed before the lamp can move, and an auto-play run banks nothing - the record this
    account archived (``another_notation``, 27 rows) is a best-of, so a worse run leaves it
    alone. ``achievementRatePercentRecord`` is written as a four-decimal string because that
    is the shape every archived row has (``100.8738``).
    """
    blocks = payload.base_score_blocks
    this_rate = achievement_rate(blocks)
    new_lamp = clear_lamp(is_cleared, blocks)
    prev_rate = float((previous or {}).get('achievementRatePercentRecord') or 0.0)
    prev_lamp = int((previous or {}).get('clearLamp') or 0)
    record_counts = not bool(payload.is_auto_play)
    rating_counts = is_cleared and record_counts
    best_rate = max(prev_rate, this_rate) if record_counts else prev_rate
    best_lamp = max(prev_lamp, new_lamp) if rating_counts else prev_lamp
    return (
        {'clearLamp': best_lamp,
         'rateGrade': rate_grade(best_rate),
         'achievementRatePercentRecord': f'{best_rate:.4f}'},
        {'rate': this_rate, 'lamp': new_lamp, 'prev_rate': prev_rate, 'prev_lamp': prev_lamp,
         'high': record_counts and this_rate > prev_rate},
    )


def install(app):
    """Route the alternate-chart finish ahead of the vendor's stub."""
    count = len(app.router.routes)
    app.include_router(router)
    app.router.routes[:] = app.router.routes[count:] + app.router.routes[:count]
