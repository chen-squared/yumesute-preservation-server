"""Publish the same short-song Ghost Live catalog to the server and the client."""

import time

from helpers.cache import cache
from helpers.msgpack import iso_to_ts, to_wire
from models.master_data import GhostLiveMaster, SenseNotationDetailMaster, SenseNotationMaster
from song_challenge import complete_short_songs


ADDED_ID_BASE = 1_000_000
TUTORIAL_SONG = 9999


def playable_songs():
    complete = complete_short_songs()
    today = time.time()
    return {music.id_ for music in cache.music_master if music.id_ in complete and music.id_ != TUTORIAL_SONG
            and music.vocal_versions and iso_to_ts(music.released_at).seconds <= today}


def _same_song_schedules():
    senses = {row.id_ for row in cache.sense_notation_master if row.details}
    songs = {row.id_: {v.vocal_version for v in row.vocal_versions or []} for row in cache.music_master}
    selected = {}
    for table in (cache.ghost_live_master, cache.concert_stage_master, cache.audition_master,
                  cache.league_master, cache.triple_cast_master):
        for row in sorted(table, key=lambda row: row.id_):
            sense = getattr(row, 'sense_notation_master_id', None) or getattr(row, 'sense_notation_master_id1', None)
            if sense in senses and row.vocal_version in songs.get(row.music_master_id, ()):
                selected.setdefault(row.music_master_id, (row.vocal_version, sense))
    return selected


def extend_cache():
    playable = playable_songs()
    schedules = _same_song_schedules()
    registered = {row.music_master_id for row in cache.ghost_live_master}
    ghost_ids = {row.id_ for row in cache.ghost_live_master}
    sense_ids = {row.id_ for row in cache.sense_notation_master}
    for music in cache.music_master:
        if music.id_ not in playable or music.id_ in registered:
            continue
        ident = ADDED_ID_BASE + music.id_
        if ident in ghost_ids:
            raise ValueError(f'Ghost Live master ID already used: {ident}')
        if music.id_ in schedules:
            vocal, sense_id = schedules[music.id_]
        else:
            vocal = min(v.vocal_version for v in music.vocal_versions)
            sense_id = ident
            if sense_id in sense_ids:
                raise ValueError(f'Ghost Live sense notation ID already used: {sense_id}')
            # The normal solo timing pattern, for a short song with no archived stage schedule.
            step = max(1, music.music_time_second // 9)
            formation = (1, 2, 3, 4, 5, 3, 2, 1)
            details = [SenseNotationDetailMaster(id_=sense_id * 10 + i, position=position,
                                                timing_second=step * i)
                       for i, position in enumerate(formation, start=1)]
            cache.sense_notation_master.append(SenseNotationMaster(id_=sense_id, details=details, buffs=[]))
        cache.ghost_live_master.append(GhostLiveMaster(
            id_=ident, music_master_id=music.id_, vocal_version=vocal, sense_notation_master_id=sense_id,
            start_date='2025-04-30T14:00:00+00:00', end_date='2100-01-01T00:00:00+00:00'))


def extend_tables(tables, changes):
    ghosts = [row for row in cache.ghost_live_master if row.id_ >= ADDED_ID_BASE]
    senses = {row.sense_notation_master_id for row in ghosts}
    for name, rows in (('GhostLiveMaster', ghosts),
                       ('SenseNotationMaster', (row for row in cache.sense_notation_master if row.id_ in senses))):
        table = tables.setdefault(name, [])
        present = {row[0] for row in table}
        for row in rows:
            if row.id_ not in present:
                table.append(to_wire(row))
                changes.append((name, row.id_, 'local-ghost-live'))
        table.sort(key=lambda row: row[0])
