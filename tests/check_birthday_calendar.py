"""Read-only verification of the birthday calendar on a running installation (port 8125).

Fetches the master the server is actually handing the client, works out which birthdays that
calendar puts on this year's dates, and then asks for exactly the media those windows need through
the same ``/production/...`` URLs the iPad uses. An open window whose media is not there would
leave the player staring at a splash that never starts, and ``/production/`` answers 404 for
anything not installed locally, so this is the check that catches it.

Touches no account and writes nothing.
"""
import asyncio
import hashlib
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor/server-of-dreams'))
sys.path.insert(0, str(ROOT / 'tools'))

import httpx
from helpers.assets import ASSETS
from helpers.config import config
from helpers.mastermemory import unpack

import birthday_calendar as born
import preservation_dates as dates

BASE = 'http://127.0.0.1:8125'
# The nine birthday movies the current addressables catalogue lists, and whose CRI film and movie
# asset are both installed: the only nine the calendar may put on this year.
PLAYABLE = {102, 104, 106, 201, 203, 302, 303, 402, 405}
# One of the twelve the catalogue has no entry for, and whose film the service had already deleted:
# the window stays closed and the client is left asking for it.
UNREACHABLE = 'happybirthday_2025_103'
WHAT = {born.SPLASH: 'movie', born.TITLE: 'title screen', born.VOICE: 'greeting'}


def fields(name):
    return born.columns(name)


def on_calendar(row, name, year):
    """This window sits on a day of ``year``, which is what the calendar is for."""
    return born.clock(row[fields(name)['start_date']]).year == year


def live_now(row, name):
    now = datetime.now(timezone.utc).timestamp()
    return row[fields(name)['start_date']].seconds <= now < row[fields(name)['end_date']].seconds


def media(name, row):
    """The bundles one open row needs, as (kind, path) pairs."""
    if name == born.SPLASH:
        value = str(row[fields(name)['splash_value']]).lower()
        return [('cri-assets', f'cridata_remote_assets_criaddressables/notification_{value}.usm.bundle'),
                ('cri-assets', f'notificationmovie_assets_movie/notification_{value}.bundle')]
    if name == born.TITLE:
        detail = fields('TitleBackgroundDetailMaster')['path_value']
        return [('2d-assets', f'title_assets_title/{str(picture[detail]).lower()}.bundle')
                for picture in row[fields(name)['details']] or []]
    return []      # the greeting's audio ships inside the app build, nothing to fetch


async def main():
    year, today = dates.serving_year(), date.today()
    async with httpx.AsyncClient(base_url=BASE, timeout=60, follow_redirects=False) as client:
        body = (await client.get('/master-data/production/preservation.db')).content
        assert len(body) > 1_000_000, f'master download looks wrong: {len(body)} bytes'
        served = unpack(body)
        archive = unpack((ROOT / 'private/upstream/master-original.db').read_bytes())
        found = born.birthday_rows(archive)[0]
        mine = {name: {row[0] for row in found[name]} for name in born.TABLES}
        was = {name: {row[0]: row for row in archive[name]} for name in born.TABLES}

        moved = {name: [row for row in served[name]
                        if row[0] in mine[name] and row != was[name][row[0]]] for name in born.TABLES}
        dated = {name: [row for row in served[name]
                        if row[0] in mine[name] and not born.permanent(row, fields(name))
                        and on_calendar(row, name, year)] for name in born.TABLES}
        open_rows = {name: [row for row in dated[name] if born.shows(name, row)]
                     for name in born.TABLES}
        assert {born.character(row) for row in open_rows[born.SPLASH]} == PLAYABLE, \
            'the calendar should hold exactly the nine reachable birthday movies on this year'
        redated = sum(1 for name in born.TABLES for row in open_rows[name]
                      if row != was[name][row[0]])
        print(f'PASS: {year} carries {len(open_rows[born.SPLASH])} birthday movies, '
              f'{len(open_rows[born.TITLE])} title screens and {len(open_rows[born.VOICE])} '
              f'greetings; {redated} of them were re-dated onto these dates by the calendar, the '
              'rest were archived that way.')

        shut = [(name, row) for name in born.TABLES for row in dated[name]
                if not born.shows(name, row)]
        for name, row in shut:
            assert row == was[name][row[0]], \
                f'{name} {row[0]} holds a {year} date and its media is not installed'
        print(f'PASS: the {len(shut)} birthday windows this client cannot load - movies whose asset '
              'the catalogue has no entry for - keep the window they shipped with.')

        if (today.month, today.day) not in {born.day_of(row[fields(born.SPLASH)['start_date']])
                                            for row in found[born.SPLASH]}:
            for name in (born.SPLASH, born.TITLE):
                open_now = [row for row in dated[name] if live_now(row, name)]
                assert not open_now, f"{today} is nobody's birthday, but {name} {open_now} is open"
            print(f'PASS: {today} is nobody\'s birthday and no birthday window is open.')

        asked = 0
        for name in (born.SPLASH, born.TITLE):
            for row in open_rows[name]:
                for kind, path in media(name, row):
                    local_file = ASSETS / kind / 'ios' / path
                    assert local_file.is_file(), f'{path} is on the calendar but not installed'
                    want = hashlib.sha256(local_file.read_bytes()).hexdigest()
                    response = await client.get(
                        f'/production/{kind}/iOS/{config["asset_version"]}/{path}')
                    assert response.status_code == 200, f'{path} -> {response.status_code}'
                    assert hashlib.sha256(response.content).hexdigest() == want, path
                    asked += 1
        print(f'PASS: {asked} birthday media files served byte-identical to the local archive.')

        for row in found[born.SPLASH]:
            if str(row[fields(born.SPLASH)['splash_value']]).lower() == UNREACHABLE:
                same = next(r for r in served[born.SPLASH] if r[0] == row[0])
                assert same == row, 'an unreachable movie should keep the window it shipped with'
        absent = await client.get('/production/cri-assets/iOS/'
                                  f'{config["asset_version"]}/notificationmovie_assets_movie/'
                                  f'notification_{UNREACHABLE}.bundle')
        assert absent.status_code == 404 and 'location' not in absent.headers, absent.status_code
        print('PASS: the movie the catalogue has no entry for answers 404 without a redirect, and '
              'its splash row stayed closed.')

        # Why those twelve cannot be opened, pinned so a later asset capture is measured against it.
        # For the nine, the ``.usm.bundle`` is CRI's own movie file: it starts with ``CRID`` and is
        # 5.2-5.7 MB. For the twelve it is the 4928-byte Unity asset the addressables group keeps in
        # place of a deleted film - a ``CriAddressablesAnchor`` naming the film's address, all twelve
        # the same size and differing only in the name inside. The film bundle that anchor points at
        # is not named anywhere in the catalogue, so the film had left the CDN before the capture:
        # there is no footage to rebuild a movie asset around.
        films = {}
        for f in sorted((ASSETS / 'cri-assets/ios/cridata_remote_assets_criaddressables')
                        .glob('notification_happybirthday_*.usm.bundle')):
            films[f.name[len('notification_happybirthday_'):-len('.usm.bundle')]] = f.read_bytes()
        gone = {k for k, v in films.items() if v[:7] == b'UnityFS'}
        assert len(gone) == 21 - len(PLAYABLE), f'{len(gone)} anchor stubs, no film'
        assert {len(films[k]) for k in gone} == {4928}, {len(films[k]) for k in gone}
        assert all(v[:4] == b'CRID' and len(v) > 5_000_000
                   for k, v in films.items() if k not in gone)
        print(f'PASS: {len(films) - len(gone)} birthday films are CRI movie files of 5.2-5.7 MB; the '
              f'other {len(gone)} are 4928-byte anchor stubs, so their movies are missing from the '
              'archive itself and cannot be rebuilt from what is here.')

        next_up = {}
        for name in born.TABLES:
            for row in open_rows[name]:
                start = born.clock(row[fields(name)['start_date']])
                when = date(start.year, start.month, start.day)
                if when >= today:
                    next_up.setdefault(when, set()).add(WHAT[name])
        print('Next on the calendar: ' + (', '.join(
            f'{when:%m-%d} {"/".join(sorted(what))}' for when, what in sorted(next_up.items())[:5])
            or f'nobody until {year + 1}'))


if __name__ == '__main__':
    asyncio.run(main())
