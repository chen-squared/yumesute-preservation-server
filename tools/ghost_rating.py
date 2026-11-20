"""Estimate a rival's performance on the chart that the player chose."""

from collections import Counter
from math import exp
from statistics import median

from helpers.cache import cache
from models.enums import ClearLamps
from ghost_schedule import playable_songs
from play_history import RATE_SCALE, WEIGHTS, rate_points, recent_clears


def _typical(rows):
    return [row['achievement'] / RATE_SCALE for row in rows]


def _spread(values):
    middle = median(values)
    return 1.48 * median(abs(value - middle) for value in values)


async def reference(s, chart):
    exact = await recent_clears(s, [chart.id_], 20)
    if len(exact) >= 5:
        values = _typical(exact)
        return median(values), _spread(values), exact

    # Prefer same difficulty and level. An adjacent level is useful when the archive
    # has very few plays here, with a small adjustment for the level difference.
    short = playable_songs()
    peers = {row.id_: row.level for row in cache.live_master
             if row.difficulty == chart.difficulty and row.music_master_id in short
             and row.id_ != chart.id_ and abs(row.level - chart.level) <= 1}
    same_level = [ident for ident, level in peers.items() if level == chart.level]
    nearby = await recent_clears(s, same_level, 60)
    best = None
    cohort = []
    adjacent = []
    for row in await s.rows('Live'):
        ident, rate = row['liveMasterId'], row['achievementRate']
        if not row['timesCompleted'] or row['clearLamp'] < int(ClearLamps.Clear) or not 0 < rate <= 101:
            continue
        if ident == chart.id_:
            best = rate
        elif ident in peers:
            level = peers[ident]
            if level == chart.level:
                cohort.append(rate)
            else:
                adjacent.append(rate + .45 * (level - chart.level))

    prior = _typical(nearby) or cohort or adjacent
    spread = _spread(prior) if prior else 0.0
    if best is not None:
        center = best - (.04 + min(.35, .12 * (101 - best)))
    elif prior:
        center = median(prior)
        if not nearby:
            center -= .08 + spread * .3  # saved bests are optimistic about a single try
    else:
        center = (98.5, 97.5, 96.0, 95.0, 92.0)[int(chart.difficulty) - 1]
    if exact:
        values = _typical(exact)
        weight = .6 + .1 * (len(values) - 1)
        center += weight * (median(values) - center)
        spread = max(_spread(values), spread * .6)
    return center, spread, exact or nearby


def simulate(notes, center, spread, samples, rng):
    """Sample legal judgment counts: a solid run, with occasional swing in form."""
    center = min(101.0, max(0.0, center))
    loss = 101.0 - center
    # Work in distance from perfect so high-level runs don't pile up at 101%.
    form = exp(rng.gauss(0.0, .14 + min(.35, spread / max(.22, loss))))
    loss = min(101.0, loss * form + rng.uniform(0.0, .018))
    # Error mixes after removing P★: stable (mostly Perfect) and more volatile.
    if center >= 100.8:
        profiles = ((.984, .015, .0008, .0001, .0001), (.95, .046, .003, .0005, .0005))
    else:
        profiles = ((.88, .105, .012, .002, .001), (.77, .17, .045, .007, .008))
    error_mix = list(rng.choice(profiles))
    observed = [sum(row['judgments'][i] for row in samples) for i in range(1, len(WEIGHTS))]
    mistakes = sum(observed)
    if mistakes:
        error_mix = [.5 * (prior + count / mistakes) for prior, count in zip(error_mix, observed)]
    losses = [101 - weight for weight in WEIGHTS[1:]]
    mean = sum(probability * amount for probability, amount in zip(error_mix, losses))
    if loss > mean:
        # A much harder chart needs more severe mistakes than this mix can provide.
        miss_share = (loss - mean) / (101 - mean)
        error_mix = [value * (1 - miss_share) for value in error_mix]
        error_mix[-1] += miss_share
        mean = loss
    probability = loss / mean
    judgment_weights = [1 - probability] + [value * probability for value in error_mix]
    counts = Counter(rng.choices(range(len(WEIGHTS)), weights=judgment_weights, k=notes))
    return rate_points([counts[i] for i in range(len(WEIGHTS))])


async def target(s, chart, rng):
    center, spread, samples = await reference(s, chart)
    # One archived medley has playable charts but zero in LiveMaster.note_count.
    # Observe its count once played, using a same-difficulty estimate before that.
    notes = chart.note_count
    if not notes:
        counts = [sum(row['judgments']) for row in samples if row['liveMasterId'] == chart.id_]
        short = playable_songs()
        comparable = [row.note_count for row in cache.live_master
                      if row.difficulty == chart.difficulty and row.music_master_id in short and row.note_count > 0]
        notes = int(median(counts or comparable or [700]))
    return simulate(notes, center, spread, samples, rng)
