"""Live-finish parity for the pinned vendor (upstream PRs #3-era fixes).

The pinned checkout predates these upstream fixes:

- 7ee917c: store ``active_live.isAutoPlay`` and never bank auto-play rates
- b6e640d: do not turn a failed run into a 0.0% record
- 289c394: keep the achievement *score* on a fail, but award the *rating*
  (live rate / clear lamp / SpRate) only on a clear
- be1020b: Olivier star-badge (SpRate) points

This module reimplements the final ``Lives/FinishAndValidate`` body locally;
``live_modes.finish`` calls it inside the existing transaction so progression
and present merging stay unchanged.
"""

from typing import Optional

from db.user import (
    delete_active_lives,
    get_lives,
    get_sp_rates,
    get_users,
    next_live_id,
    update_live_result,
    update_player_rate,
    upsert_live,
    upsert_sp_rate,
)
from helpers.cache import cache
from helpers.live_rate import chart_live_rate, chart_live_rate_result
from helpers.live_result import (
    achievement_rate,
    clear_lamp,
    good_or_worse_count,
    play_totals,
    rate_grade,
)
from helpers.live_drops import grant_frames
from helpers.music_unlock import affects_unlocks, apply_unlocks, stella_unlocked_by
from helpers.msgpack import fault, read_request, respond
from helpers.score import verify_score_blocks
from helpers.things import present_type
from helpers.user_data import build_present, current_user_id, data_object
from models import *
from models.database import LiveModel, SpRateModel
from models.enums import (
    AchievementRateGrades,
    ClearLamps,
    LiveReleaseStatus,
    MusicDifficulties,
    TimingTypes,
)
from routes import lives as upstream_lives


# --- PR #3-era helpers not present in the pinned checkout -------------------------

# base points, highest achievement-rate threshold first; columns are star levels 1..10
_SP_RATE_BASE = (
    (100.95, (60, 70, 80, 90, 100, 110, 120, 130, 140, 150)),
    (100.90, (59, 69, 79, 89, 99, 109, 118, 128, 138, 148)),
    (100.85, (58, 68, 78, 88, 98, 108, 116, 126, 136, 146)),
    (100.80, (57, 67, 77, 87, 97, 106, 114, 124, 134, 144)),
    (100.75, (56, 66, 76, 86, 96, 104, 112, 122, 132, 142)),
    (100.70, (55, 65, 75, 85, 94, 102, 110, 120, 130, 140)),
    (100.60, (54, 64, 74, 84, 92, 100, 108, 118, 128, 138)),
    (100.50, (53, 63, 73, 82, 90, 98, 106, 116, 126, 135)),
    (100.40, (52, 62, 72, 80, 88, 96, 104, 114, 124, 132)),
    (100.30, (51, 61, 70, 78, 86, 94, 102, 112, 121, 129)),
    (100.20, (50, 60, 68, 76, 84, 92, 100, 110, 118, 126)),
    (100.10, (49, 58, 66, 74, 82, 90, 98, 107, 115, 123)),
    (100.00, (48, 56, 64, 72, 80, 88, 96, 104, 112, 120)),
    (99.00, (43, 50, 57, 64, 71, 78, 85, 92, 99, 106)),
    (98.00, (41, 48, 55, 62, 68, 75, 82, 89, 96, 102)),
    (97.00, (36, 42, 48, 54, 59, 65, 71, 77, 83, 88)),
    (96.00, (34, 40, 46, 52, 56, 62, 68, 74, 80, 84)),
    (95.00, (32, 38, 44, 50, 53, 59, 65, 71, 77, 81)),
    (94.00, (27, 32, 37, 42, 44, 49, 54, 59, 64, 67)),
    (93.00, (25, 30, 35, 40, 42, 46, 51, 56, 61, 64)),
    (92.00, (23, 28, 33, 38, 40, 43, 48, 53, 58, 61)),
    (91.00, (21, 26, 31, 36, 38, 40, 45, 50, 55, 58)),
    (90.00, (19, 24, 29, 34, 36, 38, 42, 47, 52, 55)),
    (89.00, (14, 18, 22, 26, 27, 28, 31, 35, 39, 41)),
    (80.00, (5, 6, 7, 8, 9, 10, 11, 12, 13, 14)),
)
_SP_RATE_LOSS_THRESHOLDS = (10, 30, 50, 75, 100)
_TOP_N = 30


def _total_of_live_rates(rates) -> float:
    """Sum of the top 30 already-computed chart live rates."""
    vals = sorted((r for r in rates if r), reverse=True)
    return round(sum(vals[:_TOP_N]), 2)


def _non_perfect_star_count(base_score_blocks) -> int:
    return sum(
        1
        for b in (base_score_blocks or [])
        if int(b.timing_type) != int(TimingTypes.PERFECT_STAR)
    )


def _sp_rate_base(stella_lv: int, rate: float) -> int:
    for threshold, points in _SP_RATE_BASE:
        if rate >= threshold:
            return points[stella_lv - 1]
    return 0


def _sp_rate_bonus(clear_lamp: int, loss_count: int) -> int:
    bonus = 0
    if clear_lamp >= int(ClearLamps.AllPerfect):
        bonus += 3
    elif clear_lamp >= int(ClearLamps.FullCombo):
        bonus += 2
    bonus += sum(1 for t in _SP_RATE_LOSS_THRESHOLDS if loss_count <= t)
    return bonus


def _olivier_sp_rate_points(
    live_master_id: int, rate: float, clear_lamp: int, loss_count: int
) -> Optional[int]:
    lm = next((m for m in cache.live_master if m.id_ == live_master_id), None)
    if lm is None or int(lm.difficulty) != int(MusicDifficulties.Olivier):
        return None
    stella_lv = lm.level - 100
    if not 1 <= stella_lv <= 10:
        return None
    return _sp_rate_base(stella_lv, rate) + _sp_rate_bonus(clear_lamp, loss_count)


def _is_long_version(live_master_id: int) -> bool:
    lm = next((m for m in cache.live_master if m.id_ == live_master_id), None)
    if lm is None:
        return False
    music = next((m for m in cache.music_master if m.id_ == lm.music_master_id), None)
    return bool(music.is_long_version) if music is not None else False


async def finish(request):
    """Same contract as upstream ``lives_finish_and_validate``, with the later fixes."""
    app = request.app
    user_id = current_user_id(request)
    payload = await read_request(request, FinishLivePayload)
    if payload is None:
        return respond(FinishLiveResult())
    if not verify_score_blocks(payload):
        return respond(FinishLiveResult(), faults=[fault("InvalidScoreBlockHash")])
    if user_id is None:
        return respond(FinishLiveResult())

    score, is_cleared = play_totals(payload)
    new_lamp = clear_lamp(is_cleared, payload.base_score_blocks)
    this_rate = achievement_rate(payload.base_score_blocks)

    async with app.acquire_db() as conn:
        user = await conn.fetchrow(get_users(user_id))
        # raw row: the pinned vendor model has no isAutoPlay field
        active = await conn.conn.fetchrow(
            'SELECT * FROM active_live WHERE "userId"=$1', user_id
        )
        live_master_id = active["liveMasterId"] if active is not None else 0
        lives = await conn.fetch(get_lives(user_id)) if live_master_id else []
        existing = next((r for r in lives if r.liveMasterId == live_master_id), None)

        before_lamp = (
            existing.clearLamp if existing is not None else int(ClearLamps.None_)
        )
        prev_rate = existing.achievementRate if existing is not None else 0.0
        auto_play = bool(active["isAutoPlay"]) if active is not None else False
        score_counts = not auto_play
        rating_counts = is_cleared and not auto_play
        is_high_score = score_counts and this_rate > prev_rate

        best_lamp = max(before_lamp, new_lamp) if rating_counts else before_lamp
        best_rate = max(prev_rate, this_rate) if score_counts else prev_rate
        best_grade = rate_grade(best_rate)
        effective_rate = this_rate if rating_counts else prev_rate
        times = (existing.timesCompleted if existing is not None else 0) + 1
        notation_rate = (
            (chart_live_rate(live_master_id, best_rate) or 0.0)
            if rating_counts
            else (existing.notationRate if existing is not None else 0.0)
        )

        live_id = existing.id if existing is not None else 0
        live_status = (
            existing.status if existing is not None else int(LiveReleaseStatus.None_)
        )
        if existing is not None:
            await conn.execute(
                update_live_result(
                    user_id,
                    live_master_id,
                    times,
                    best_rate,
                    notation_rate,
                    best_lamp,
                    best_grade,
                )
            )
        elif live_master_id and score_counts:
            id_row = await conn.fetchrow(next_live_id())
            live_id = id_row.value if id_row is not None else live_master_id
            await conn.execute(
                upsert_live(
                    user_id,
                    {
                        "id": live_id,
                        "liveMasterId": live_master_id,
                        "timesCompleted": times,
                        "achievementRate": best_rate,
                        "notationRate": notation_rate,
                        "clearLamp": best_lamp,
                        "status": live_status,
                        "rateGrade": best_grade,
                    },
                )
            )

        await conn.execute(delete_active_lives(user_id))

        present: list = []
        if live_master_id and (existing is not None or score_counts):
            present.append(
                data_object(
                    "Live",
                    LiveModel(
                        userId=user_id,
                        id=live_id,
                        liveMasterId=live_master_id,
                        timesCompleted=times,
                        achievementRate=best_rate,
                        notationRate=notation_rate,
                        clearLamp=best_lamp,
                        status=live_status,
                        rateGrade=best_grade,
                    ),
                )
            )

        if affects_unlocks(live_master_id):
            changed = await apply_unlocks(
                conn,
                user_id,
                stella_unlocked_by(
                    live_master_id,
                    is_cleared,
                    good_or_worse_count(payload.base_score_blocks),
                ),
            )
            present += [data_object("Music", music) for music in changed]

        live_drops: list[LiveDropThing] = []
        if not _is_long_version(live_master_id):
            setting_id = active["liveSettingMasterId"] if active is not None else 0
            frames = upstream_lives.resolve_frames(
                setting_id,
                stamina_consumed=(
                    active["staminaSpent"] if active is not None else False
                ),
                score=score,
                star_act_count=len(payload.star_act_score_blocks or []),
                achievement_rate=this_rate,
            )
            live_drops = await grant_frames(conn, user_id, frames)

        lr_best, lr_this = chart_live_rate_result(
            live_master_id, effective_rate, prev_rate
        )
        live_rate_result = RateUpdateResult(best_ever=lr_best, this_time=lr_this)
        total_before = _total_of_live_rates(r.notationRate for r in lives)
        after_notations = [
            r.notationRate for r in lives if r.liveMasterId != live_master_id
        ]
        if live_master_id and (existing is not None or score_counts):
            after_notations.append(notation_rate)
        total_after = _total_of_live_rates(after_notations)
        if total_after != total_before:
            await conn.execute(update_player_rate(user_id, total_after))

        sp_rate_update_result = None
        this_points = (
            _olivier_sp_rate_points(
                live_master_id,
                this_rate,
                new_lamp,
                _non_perfect_star_count(payload.base_score_blocks),
            )
            if rating_counts
            else None
        )
        if this_points is not None:
            sp_rows = await conn.fetch(get_sp_rates(user_id))
            existing_sp = next(
                (r for r in sp_rows if r.liveMasterId == live_master_id), None
            )
            before_best = existing_sp.point if existing_sp is not None else 0
            before_total = sum(r.point for r in sp_rows)
            new_best = max(before_best, this_points)
            after_total = before_total - before_best + new_best
            if existing_sp is not None:
                sp_id = existing_sp.id
                if new_best != before_best:
                    await conn.conn.execute(
                        'UPDATE "sp_rate" SET "point"=$3 '
                        'WHERE "userId"=$1 AND "id"=$2',
                        user_id,
                        sp_id,
                        new_best,
                    )
            else:
                sp_id = max((r.id for r in sp_rows), default=0) + 1
                await conn.execute(
                    upsert_sp_rate(
                        user_id,
                        {
                            "id": sp_id,
                            "liveMasterId": live_master_id,
                            "point": new_best,
                        },
                    )
                )
            sp_rate_update_result = SpRateUpdateResult(
                best_ever=before_best,
                this_time=this_points,
                best_ever_total=before_total,
                this_time_total=after_total,
            )
            present.append(
                data_object(
                    "SpRate",
                    SpRateModel(
                        userId=user_id,
                        id=sp_id,
                        liveMasterId=live_master_id,
                        point=new_best,
                    ),
                )
            )

    refresh: set = set()
    if live_drops:
        refresh |= {present_type(int(d.received_thing.type)) for d in live_drops}
        refresh.discard(None)
    if total_after != total_before:
        refresh.add("UserProfile")
    if refresh:
        present += await build_present(app, user_id, *sorted(refresh))

    result_lamp = new_lamp if score_counts else before_lamp
    display_rate = this_rate if score_counts else prev_rate
    result = FinishLiveResult(
        clear_lamp=ClearLamps(result_lamp),
        before_clear_lamp=ClearLamps(before_lamp),
        rate_grade=AchievementRateGrades(rate_grade(display_rate)),
        is_high_score=is_high_score,
        achievement_rate_average=0.0,
        rate_result=RateResult(
            achievement_rate_result=RateUpdateResult(
                best_ever=round(prev_rate, 4),
                this_time=round(display_rate, 4),
            ),
            live_rate_result=live_rate_result,
            total_rate_before=total_before,
            total_rate_after=total_after,
        ),
        sp_rate_update_result=sp_rate_update_result,
        player_rank_point_result=PlayerRankPointResult(
            rank_before=user.playerRank,
            rank_after=user.playerRank,
            rank_point_before=user.currentRankPoint,
            rank_point_after=user.currentRankPoint,
            rank_point_acquired=0,
            stamina_before=user.currentStamina,
        ),
        live_drop_things=live_drops,
        league_rewards=[],
        audition_rewards=[],
        story_event_rewards=[],
        accessory_auto_sell_convert_things=[],
    )
    return respond(result, present=present)


def install() -> None:
    """Make live_modes.finish call this implementation."""
    upstream_lives.lives_finish_and_validate = finish
