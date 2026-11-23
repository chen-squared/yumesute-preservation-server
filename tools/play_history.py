"""Recent settled results, keyed by the actual chart (including alternate charts)."""

from collections import Counter
from dataclasses import dataclass
import time

from helpers.live_result import play_totals
from models.enums import TimingTypes


TABLE = 'preservation_play_history'
PER_CHART = 20
RATE_SCALE = 10_000
WEIGHTS = (101, 100, 80, 50, 0, 0)
TIMINGS = (TimingTypes.PERFECT_STAR, TimingTypes.PERFECT, TimingTypes.GREAT,
           TimingTypes.GOOD, TimingTypes.BAD, TimingTypes.MISS)


def rate_points(judgments):
    """Percent to four decimal places as an integer, with exact half-up rounding."""
    notes = sum(judgments)
    weighted = sum(count * weight for count, weight in zip(judgments, WEIGHTS))
    return (weighted * RATE_SCALE + notes // 2) // notes if notes else 0


@dataclass(frozen=True)
class Play:
    judgments: tuple[int, ...]
    score: int
    cleared: bool
    max_combo: int

    @property
    def points(self):
        return rate_points(self.judgments)


def summarize(payload):
    blocks = payload.base_score_blocks or []
    if not blocks:
        return None
    counts = Counter(int(block.timing_type) for block in blocks)
    judgments = [counts.pop(int(timing), 0) for timing in TIMINGS]
    judgments[-1] += sum(counts.values())  # unjudged notes carry zero weight
    score, cleared = play_totals(payload)
    return Play(tuple(judgments), score, cleared, max(int(block.combo) for block in blocks))


async def ensure_table(conn):
    await conn.conn.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        "userId" bigint NOT NULL REFERENCES accounts("userId") ON DELETE CASCADE,
        "liveMasterId" bigint NOT NULL,
        "anotherNotationMasterId" bigint NOT NULL,
        mode text NOT NULL,
        "finishedAt" bigint NOT NULL,
        "isAutoPlay" boolean NOT NULL,
        cleared boolean NOT NULL,
        achievement integer NOT NULL CHECK (achievement BETWEEN 0 AND 1010000),
        "rivalAchievement" integer CHECK ("rivalAchievement" BETWEEN 0 AND 1010000),
        judgments integer[] NOT NULL,
        score bigint NOT NULL,
        "maxCombo" integer NOT NULL)''')
    # Let installs that already collected Star Acts retain those values and omit the field on new plays.
    if await conn.conn.fetchval('''SELECT attnotnull FROM pg_attribute
            WHERE attrelid = to_regclass($1) AND attname = 'starActs' AND NOT attisdropped''', TABLE):
        await conn.conn.execute(f'ALTER TABLE {TABLE} ALTER COLUMN "starActs" DROP NOT NULL')
    await conn.conn.execute(f'CREATE INDEX IF NOT EXISTS preservation_play_history_chart '
                            f'ON {TABLE} ("userId","liveMasterId","anotherNotationMasterId",id DESC)')


async def record(s, live_id, mode, play, *, auto=False, alternate=0, rival=None):
    if play is None or not (live_id or alternate):
        return
    # Some standalone checks call the API without starting FastAPI's lifespan.
    if not await s.conn.conn.fetchval('SELECT to_regclass($1)', TABLE):
        return
    await s.conn.conn.execute(f'''INSERT INTO {TABLE}
        ("userId","liveMasterId","anotherNotationMasterId",mode,"finishedAt","isAutoPlay",cleared,
         achievement,judgments,score,"maxCombo","rivalAchievement")
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)''',
        s.uid, live_id, alternate, mode, time.time_ns() // 1000, bool(auto), play.cleared,
        play.points, play.judgments, play.score, play.max_combo, rival)
    await s.conn.conn.execute(f'''DELETE FROM {TABLE} WHERE id IN (
        SELECT id FROM {TABLE} WHERE "userId"=$1 AND "liveMasterId"=$2 AND "anotherNotationMasterId"=$3
        ORDER BY id DESC OFFSET $4)''', s.uid, live_id, alternate, PER_CHART)


async def recent_clears(s, live_ids, limit):
    return await s.conn.conn.fetch(f'''SELECT "liveMasterId",achievement,judgments,"finishedAt" FROM {TABLE}
        WHERE "userId"=$1 AND "liveMasterId"=ANY($2::bigint[]) AND "anotherNotationMasterId"=0
        AND cleared AND NOT "isAutoPlay" ORDER BY id DESC LIMIT $3''', s.uid, live_ids, limit)
