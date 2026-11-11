"""Generated LeagueMaster / TripleCastMaster rows, so the ladder keeps running.

The recovered archive's ladder stops with the service: league window 165 and triple
cast window 3037 both close on 2026-09-29. Every ingredient of a generated window is
official - the song, the bonus-character sense notation, the vocal version, the class
ladder, the per-play reward package - and only two things come from us: the id and the
calendar.

The calendar is measured off the archive rather than invented. Across all 165 archived
LeagueMaster rows the display period is exactly seven days opening and closing at
14:00 JST on a Tuesday, and ``counting_start_at`` is always 29 hours before
``display_end_at`` (Mon 09:00 JST in every row). The 37 TripleCastMaster rows are
fourteen days with the same 29 hour counting tail. So the generated chain simply
continues the phase of the last official window, stepping the official period, and a
window's id is its position in that chain - stable across restarts, which matters
because league entities reference the window id.

Songs are dealt out of the official pool in shuffled cycles, so a song never repeats
inside a cycle, and each song carries the sense-notation set (and the play-reward
package) that an official window used with it. Only the pairings the ladder itself
settled on are dealt out - see usable() for why 48 of the archived league pairings
name two different songs in the two places the client reads them from.
"""
import datetime as dt
import json
import random
import time
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import msgpack
from helpers.cache import cache
from models.keys import KEYS

MASTERDATA = Path(__file__).resolve().parents[1] / 'vendor/server-of-dreams/_data/masterdata'

JST = dt.timezone(dt.timedelta(hours=9))
DAY = 86400

# Measured on the archive: counting_start_at == display_end_at - 29h for all 202 rows.
COUNTING_LEAD = 29 * 3600

# Closed windows stay published this long, so a ladder reward can still be paid out
# after the window ends while the app has been offline.
PUBLISHED_GRACE = 120 * DAY

# How far ahead the chain is published.
AHEAD_DAYS = 42

KINDS = {
    'league': {
        'table': 'league_master',
        'model': 'LeagueMaster',
        'step': 7 * DAY,
        'sense': ('sense_notation_master_id',),
    },
    'triple_cast': {
        'table': 'triple_cast_master',
        'model': 'TripleCastMaster',
        'step': 14 * DAY,
        'sense': ('sense_notation_master_id1', 'sense_notation_master_id2',
                  'sense_notation_master_id3'),
    },
}


class Window(NamedTuple):
    kind: str
    id: int
    music: int
    difficulty: int
    sense_ids: tuple
    vocal_version: int
    display_start: int
    counting_start: int
    display_end: int
    class_group: int
    season: int
    official: bool
    # The archived window this one's song/bonus/rewards were taken from.
    source: int = 0

    def phase(self, now):
        if now >= self.display_end:
            return 'closed'
        if now >= self.counting_start:
            return 'counting'
        if now >= self.display_start:
            return 'display'
        return 'upcoming'


def _seconds(value):
    if isinstance(value, (int, float)):
        return int(value)
    return int(dt.datetime.fromisoformat(value).timestamp())


def iso(seconds):
    """The timestamp form the master data uses."""
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat()


_iso = iso


@lru_cache(maxsize=None)
def archived(kind):
    """The ladder exactly as the archive shipped it.

    Read from the master JSON rather than the in-process cache, so the generated chain
    is the same whether it is asked for while packing the master download or while
    answering a request.
    """
    return json.loads((MASTERDATA / f"{KINDS[kind]['model']}.json").read_text(encoding='utf-8'))


def _window(kind, row):
    spec = KINDS[kind]
    season = 'league_season_master_id' if kind == 'league' else 'triple_cast_season_master_id'
    return Window(
        kind=kind,
        id=int(row['id']),
        music=int(row['music_master_id']),
        difficulty=int(row['max_difficulty']),
        sense_ids=tuple(int(row[field]) for field in spec['sense']),
        vocal_version=int(row['vocal_version']),
        display_start=_seconds(row['display_start_at']),
        counting_start=_seconds(row['counting_start_at']),
        display_end=_seconds(row['display_end_at']),
        class_group=int(row['league_class_group_master_id']),
        season=int(row[season]),
        official=True,
    )


def official(kind):
    """The archived ladder, in time order."""
    return sorted((_window(kind, row) for row in archived(kind)), key=lambda w: w.display_start)


def anchor(kind):
    """The last official window: its id and start phase seed the generated chain."""
    return official(kind)[-1]


def songs(kind):
    """music -> [(source window id, sense ids, vocal version)] used by the archive.

    Every pairing, including the ones a generated window must not copy; usable() is the
    one the ladder draws from.
    """
    pairs = {}
    for row in official(kind):
        pairs.setdefault(row.music, []).append((row.id, row.sense_ids, row.vocal_version))
    return pairs


@lru_cache(maxsize=None)
def titles():
    """music id -> (title of the song row, {vocal version: title of that arrangement}).

    The ladder's entry card is drawn from MusicMaster, which is what the ladder's
    TopMenuInformation hands over as ``music_master_id``. The ladder page instead resolves
    the window's own ``vocal_version`` in that row's MusicVocalVersionMaster list, and that
    arrangement can carry a different title.
    """
    found = {}
    for row in json.loads((MASTERDATA / 'MusicMaster.json').read_text(encoding='utf-8')):
        found[int(row['id'])] = (row['name'],
                                 {int(one['vocal_version']): one['name']
                                  for one in row.get('vocal_versions') or []})
    return found


def one_song(row):
    """True when a window on this song shows one title rather than two.

    Measured on the archive: all 37 triple cast windows and 117 of the 165 league windows -
    every league window from id 49 on - name a 10000-series (Long Ver.) song row at
    ``vocal_version`` 1, where the song title and the arrangement title are the same string.
    The 48 older league rows instead name the short song row and put the longer arrangement
    on ``vocal_version`` 2, whose title adds a （Long Ver） suffix, so the card outside reads
    情熱リベレイション while the page inside the same window reads 情熱リベレイション（Long Ver）.
    """
    card, arrangements = titles().get(row.music, ('', {}))
    return arrangements.get(row.vocal_version) == card


def usable(kind):
    """The pairings a generated window may copy, by music.

    That is the archived pairings whose song reads the same in both places (one_song),
    which is also exactly the pairing the archived ladder has used since it settled: the
    league's own song list has been 10000-series songs at ``vocal_version`` 1 for the last
    117 windows, and the triple cast ladder never used anything else.
    """
    pairs = {}
    for row in official(kind):
        if one_song(row):
            pairs.setdefault(row.music, []).append((row.id, row.sense_ids, row.vocal_version))
    return pairs


def pool(kind):
    """The songs a generated window can be given, in a stable order."""
    return sorted(usable(kind))


def _deal(kind, position, dealings):
    """Song for the position-th generated window: shuffled cycles, no repeats inside one."""
    size = len(dealings)
    cycle, index = divmod(position, size)
    deck = list(dealings)
    random.Random(f'{kind}:deck:{cycle}').shuffle(deck)
    return deck[index % size]


def generated(kind, now=None):
    """The chain that carries the ladder past the archive's last window.

    The phase comes from the archived ladder itself - every window in it opens at a
    fixed offset that is a whole number of steps from the last regular one - and the
    first generated window is the first of those slots that is not already taken by an
    archived window. The league archive's last row is the exception that makes this
    necessary: window 165 overran its week to 2026-09-29 23:00 JST because that is when
    the service stopped, so the weekly Tuesday 14:00 JST phase resumes on 10-06 rather
    than colliding with it.
    """
    spec = KINDS[kind]
    last = anchor(kind)
    dealings = pool(kind)
    horizon = (now if now is not None else _now()) + AHEAD_DAYS * DAY
    slot = 1
    while last.display_start + slot * spec['step'] < last.display_end:
        slot += 1
    out, window_index = [], 1
    while last.display_start + slot * spec['step'] < horizon:
        start = last.display_start + slot * spec['step']
        end = start + spec['step']
        window_id = last.id + window_index
        music = _deal(kind, window_index - 1, dealings)
        source, sense, vocal = random.Random(f'{kind}:song:{window_id}').choice(usable(kind)[music])
        out.append(Window(
            kind=kind,
            id=window_id,
            music=music,
            difficulty=last.difficulty,
            sense_ids=sense,
            vocal_version=vocal,
            display_start=start,
            counting_start=end - COUNTING_LEAD,
            display_end=end,
            class_group=last.class_group,
            season=last.season,
            official=False,
            source=source,
        ))
        slot += 1
        window_index += 1
    return out


def _now():
    return int(time.time_ns() // 1_000_000_000)


def published(kind, now=None):
    """Everything the client should be able to see: the archive plus the live chain."""
    now = now if now is not None else _now()
    fresh = [w for w in generated(kind, now) if w.display_end > now - PUBLISHED_GRACE]
    return official(kind) + fresh


def live(kind, now=None):
    now = now if now is not None else _now()
    return next((w for w in published(kind, now) if w.display_start <= now < w.display_end), None)


def find(kind, window_id, now=None):
    return next((w for w in published(kind, now) if w.id == int(window_id)), None)


def closed(kind, now=None):
    now = now if now is not None else _now()
    return [w for w in published(kind, now) if w.display_end <= now]


# --- injection: the client reads these through the master data download -------------

def json_rows(kind):
    """Generated rows as master-data JSON dicts (for the in-process cache)."""
    return [
        {
            'id': w.id,
            'music_master_id': w.music,
            'max_difficulty': w.difficulty,
            'display_start_at': _iso(w.display_start),
            'counting_start_at': _iso(w.counting_start),
            'display_end_at': _iso(w.display_end),
            'league_class_group_master_id': w.class_group,
            **{field: value for field, value in zip(KINDS[kind]['sense'], w.sense_ids)},
            'vocal_version': w.vocal_version,
            ('league_season_master_id' if kind == 'league' else 'triple_cast_season_master_id'): w.season,
            **({} if kind == 'league' else {
                'vocal_effect_master_id': 0, 'expression_effect_master_id': 0,
                'concentration_effect_master_id': 0,
            }),
        }
        for w in generated(kind)
    ]


def play_reward_rows(kind):
    """Per-play rewards for the generated ids, copied from each window's source."""
    packages = {
        row['id']: row
        for row in json.loads((MASTERDATA / 'LeaguePlayRewardPackageMaster.json')
                              .read_text(encoding='utf-8'))
    }
    rows = []
    for w in generated(kind):
        source = packages.get(w.source)
        if source is None:
            continue
        rows.append({
            'id': w.id,
            'rewards': [
                {'thing_id': thing['thing_id'], 'thing_type': int(thing['thing_type']),
                 'thing_quantity': thing['thing_quantity']}
                for thing in source.get('rewards') or []
            ],
        })
    return rows


def packed(name, values):
    """A master-data dict in the positional list form the packed master db uses."""
    keys = KEYS[name]
    row = [None] * (max(k[0] for k in keys) + 1)
    for key, field, base, array, kind_of, *_ in keys:
        # The models alias the primary key id -> id_, the master JSON does not.
        value = values.get(field, values.get(field.rstrip('_')))
        if value is None:
            continue
        if base == 'DateTime':
            stamp = _seconds(value)
            row[key] = msgpack.Timestamp(stamp, 0)
        elif kind_of == 'model':
            row[key] = [packed(base, x) for x in (value if array else [value])]
        else:
            row[key] = value
    return row


def extend_tables(tables, changes):
    """Append the generated windows to an unpacked master db."""
    for kind, spec in KINDS.items():
        name = spec['model']
        for row in json_rows(kind):
            tables[name].append(packed(name, row))
            changes.append((name, row['id'], 'generated-window'))
    play = 'LeaguePlayRewardPackageMaster'
    for kind in KINDS:
        for row in play_reward_rows(kind):
            tables[play].append(packed(play, row))
            changes.append((play, row['id'], 'generated-play-rewards'))


def extend_cache():
    """Make the generated windows visible to master() in this process."""
    from models.master_data import LeaguePlayRewardPackageMaster, LeagueMaster, TripleCastMaster

    models = {'LeagueMaster': LeagueMaster, 'TripleCastMaster': TripleCastMaster,
              'LeaguePlayRewardPackageMaster': LeaguePlayRewardPackageMaster}
    for kind, spec in KINDS.items():
        table = KINDS[kind]['table']
        rows = getattr(cache, table)
        known = {getattr(row, 'id_') for row in rows}
        for row in json_rows(kind):
            if row['id'] not in known:
                rows.append(models[spec['model']].model_validate(row))
    packages = cache.league_play_reward_package_master
    known = {p.id_ for p in packages}
    for kind in KINDS:
        for row in play_reward_rows(kind):
            if row['id'] not in known:
                packages.append(models['LeaguePlayRewardPackageMaster'].model_validate(row))
