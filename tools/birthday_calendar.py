"""Move the official character-birthday calendar onto the year being lived through.

Birthday content is master-only: the client decides on its own clock which birthday splash,
title screen and title greeting is live, and never tells the server. The only splash call in the
whole API is ``Player/UpdateSplashLastDisplayTime``, which records when the last splash was shown
(the account's ``splashLastDisplayedAt``), so re-dating a window is the whole feature - nothing
has to be invented. The recovered master already carries the content; only the windows are past.

What one character's birthday is made of, measured in the recovered master:

``SplashMaster``
    21 rows with ``splash_type`` Video (1) and ``splash_value`` ``HappyBirthday_<edition>_<character>`` -
    one per character, 9 named for the 2025 edition and 12 for the 2026 - each open for a single day,
    and each naming the movie it plays: ``notification_<splash_value>.usm.bundle``. This is the
    birthday animation.
``TitleBackgroundMaster``
    50 rows whose detail ``path_value`` is ``Title_Birthday_<name>_<edition>``: the title screen for
    that day. The 2026 birthdays are re-runs in the master's own style - separate rows naming the
    earlier pictures again - so a second year of the same birthday reuses archived art.
``TitleCallVoiceMaster``
    The greeting spoken on that title screen: 26 rows. Two measured facts tie a row to a character -
    the ``voice_id`` is the character id with 501 on the end (103501 is Kathrina's) and the row opens
    on that character's birthday - and that catches the four 2026 re-runs of the 2025 edition's
    greetings, which the splash's own window would miss, and 静香's 2026 line, which opens an hour
    after midnight. One of the 26 (row 102, 静香's line once more) runs from 2024-07-26 to the 2100
    sentinel at priority 0, so it is live every day as archived; it is left exactly as it is. The
    archive holds 30 more greeting rows that are not birthdays - a set naming every character on
    2024-07-26, and two sets of 9xx ids in November 2024 - and those keep the windows they shipped
    with. Their audio is not a CDN asset at all: it is inside the app build, in
    ``Data/Raw/Sounds/TitleCall.acb`` of 2.31.3, so nothing has to be downloaded for them.

The rule is one field: keep the month, day, hour and minute, replace only the year, with the year
the master is being served in. Each character's content then opens on the date it opened officially
and closes the next day, and on every other day of the year the served master is byte for byte what
was recovered. Birthday dates are keyed by year, so the master version the
client sees changes for the birthday calendar once a year. League/course publication
also advances the master version as their rolling horizons grow.

Only the newest edition of a date is opened (``editions``): a re-run is its own row naming the older
picture again, and putting two pictures on the title screen for the same day is the client's problem
to decide, not ours. A year therefore holds 55 birthday windows - 9 movies, 25 title screens of the
50 rows and 21 greetings of the 26. A row that already carries that year's date is left byte for byte
as it is and is not reported as a change, which is why in 2026 only the title screens and greetings
move: the nine movies, the newest edition, were archived already dated 2026.

Only the part of the calendar the archive can actually show is opened. The current addressables
catalogue (``data/catalogs/cri-assets.json.br``, asset version 1.96.0) lists nine birthday movies -
``Notification_HappyBirthday_2026_102/104/106/201/203/302/303/402/405`` - and those are exactly the
nine whose film and movie asset are both on disk. For the other twelve the service had thrown the
film away before the capture: their ``.usm.bundle`` is a 4928-byte Unity asset that holds a
``CriAddressablesAnchor`` and the address ``notification_happybirthday_<value>.usm`` and nothing else
- all twelve the same size, differing only in the name written inside - while the nine films are
5.2-5.7 MB and start with CRI's ``CRID`` magic. The catalogue names no movie asset for the twelve
either, and the film bundle their anchor points at (``b80ad8463764cd1cdc95ad2ec864d108.bundle`` for
2025_103) is nowhere in it, so the film was gone from the CDN before anything was archived and there
is nothing to reconstruct them from. Those splash rows stay closed, and that character's title screen
and greeting still move. ``movies()`` reports the split, so a later asset capture can be measured
against it.

``YUMESUTE_BIRTHDAY=<characterId>`` stands one character's birthday up from the moment the server
starts instead of waiting for the date. It is a viewing aid for checking the feature in game: the
rows, the media and the one-day length are the official ones, only the window is opened now, and it
starts at start-up because the client only plays a splash whose window opened after the last one it
showed. Unset it, and the master holds nothing but the rotation.
"""
import datetime
import os
import time
from pathlib import Path

import msgpack
from models.keys import KEYS

ROOT = Path(__file__).resolve().parents[1]
# The folders the server itself reads bundles out of (helpers/assets.py), which is what decides
# whether a client can load a window that is open. The download area under data/assets holds the
# same files, but it is the staging copy, not the served one.
ASSETS = ROOT / 'vendor/server-of-dreams/_data/assets'
MOVIE = ASSETS / 'cri-assets/ios/cridata_remote_assets_criaddressables'
MOVIE_ASSET = ASSETS / 'cri-assets/ios/notificationmovie_assets_movie'
ART = ASSETS / '2d-assets/ios/title_assets_title'

SPLASH, TITLE, VOICE = 'SplashMaster', 'TitleBackgroundMaster', 'TitleCallVoiceMaster'
TABLES = (SPLASH, TITLE, VOICE)
# SplashTypes.Video: the splash plays the movie named by splash_value.
MOVIE_TYPE = 1
MOVIE_PREFIX = 'HappyBirthday_'
ART_PREFIX = 'Title_Birthday_'
# 2100-01-01: the master's own "no end" value, the same one preservation_dates pushes windows to.
# A row that already uses it is open every day and needs no calendar - and moving its end back to
# January of the serving year would close it, which is the opposite of the point.
PERMANENT_END = 4102444800


def columns(table):
    return {field: key for key, field, *_ in KEYS[table]}


def clock(stamp):
    """A master DateTime as the wall clock it is stored to be."""
    return datetime.datetime.fromtimestamp(stamp.seconds, datetime.timezone.utc)


def movie(archived_value):
    """Both files a splash movie needs: CRI's film and the movie asset that names it. For the twelve
    the service deleted, the second is the one that is missing; the first is an anchor stub."""
    name = str(archived_value).lower()
    return ((MOVIE / f'notification_{name}.usm.bundle').exists()
            and (MOVIE_ASSET / f'notification_{name}.bundle').exists())


def pictures(details):
    """Every picture a title-background row names is archived."""
    path_value = columns('TitleBackgroundDetailMaster')['path_value']
    return bool(details) and all((ART / f'{str(d[path_value]).lower()}.bundle').exists()
                                 for d in details)


def birthday_rows(tables):
    """The birthday rows by table, plus which characters have a birthday on each date."""
    splash, title, voice = (columns(name) for name in TABLES)
    detail = columns('TitleBackgroundDetailMaster')['path_value']
    found, on = {name: [] for name in TABLES}, {}
    for row in tables.get(SPLASH, ()):
        value = row[splash['splash_value']]
        if int(row[splash['splash_type']] or 0) == MOVIE_TYPE and str(value).startswith(MOVIE_PREFIX):
            found[SPLASH].append(row)
            on.setdefault(day_of(row[splash['start_date']]), set()).add(character(row))
    for row in tables.get(TITLE, ()):
        details = row[title['details']] or []
        if any(str(d[detail]).startswith(ART_PREFIX) for d in details):
            found[TITLE].append(row)
    for row in tables.get(VOICE, ()):
        if int(row[voice['voice_id']]) // 1000 in on.get(day_of(row[voice['start_date']]), set()):
            found[VOICE].append(row)
    return found, on


def day_of(stamp):
    """The calendar date a window opens on, in the master's wall clock."""
    moment = clock(stamp)
    return (moment.month, moment.day)


def shows(table, row):
    """Would opening this window show something the client can load?"""
    if table == SPLASH:
        return movie(row[columns(SPLASH)['splash_value']])
    if table == TITLE:
        return pictures(row[columns(TITLE)['details']])
    return True     # the title greeting's audio is inside the app build


def character(row):
    """The character id a birthday splash row belongs to (``HappyBirthday_2026_102`` -> 102)."""
    head, _, tail = str(row[columns(SPLASH)['splash_value']]).rpartition('_')
    return int(tail) if head.startswith(MOVIE_PREFIX) and tail.isdigit() else None


def movies(tables):
    """``{character id: the movie is archived}`` for every birthday splash row."""
    return {character(row): movie(row[columns(SPLASH)['splash_value']])
            for row in birthday_rows(tables)[0][SPLASH]}


def move(stamp, year):
    """The same month, day and hour of ``stamp`` in ``year``, nanoseconds kept."""
    if not isinstance(stamp, msgpack.Timestamp):
        return None
    moment = datetime.datetime.fromtimestamp(stamp.seconds, datetime.timezone.utc)
    try:
        moved = moment.replace(year=year)
    except ValueError:      # a 29 February birthday in a year without one
        return None
    return msgpack.Timestamp(int(moved.timestamp()), stamp.nanoseconds)


def pinned():
    """``YUMESUTE_BIRTHDAY=<characterId>``: stand one birthday up on today.

    A viewing aid, not part of the calendar. The rows and the media stay official; only the window
    is stood up now, so a device can be pointed at one character's birthday on a date the real
    calendar would not show it - which is how this feature gets checked in game. Unset, as it is
    for normal play, and the calendar is only the rotation above.
    """
    value = os.environ.get('YUMESUTE_BIRTHDAY', '').strip()
    return int(value) if value.isdigit() else None


def fresh():
    """A window start the client cannot have seen yet: now, at the moment this master was built.

    The client keeps the instant the last splash played (the account's ``splashLastDisplayedAt``) and
    only shows a splash whose window opened after it, so the official midnight boundary would be
    spent on the first look. Starting the stood-up window at build time means one restart arms it
    again, and the window is still the official one day long.
    """
    return int(time.time())


def birthday_days(found, whose, fields):
    """The official dates that belong to one character's birthday.

    Only the splash row says which character it is; the title screen and the greeting are tied to
    that character by the date they open on, which is the only link the master has.
    """
    splash = fields[SPLASH]
    return {day_of(row[splash['start_date']]) for row in found[SPLASH] if character(row) == whose}


def permanent(row, fields):
    """This row is open every day already (its end is the master's 2100 sentinel)."""
    return row[fields['end_date']].seconds >= PERMANENT_END


def editions(found):
    """For each date, the rows of the newest edition the service reached for it.

    A re-run is its own row naming the older picture again, so one birthday date can hold two or
    three years of the same content in the master. Only the newest of those is worth opening: all of
    them on one day would give the client two title pictures for the same date to choose between,
    and the older ones are the state of a year that has already been lived through.
    """
    newest = {}
    for name, rows in found.items():
        start = columns(name)['start_date']
        by_day = {}
        for row in rows:
            by_day.setdefault(day_of(row[start]), []).append(row)
        newest[name] = [row for group in by_day.values() for row in group
                        if row[start].seconds == max(x[start].seconds for x in group)]
    return newest


def rotate(tables, changes, year, watch=None):
    """Move every birthday window that has archived media onto ``year``.

    ``watch`` is the optional viewing aid from ``pinned()``: on top of the rotation, that
    character's splash, title screen and greeting are open from start-up for one day, and are
    official in every other way.
    """
    fields = {name: columns(name) for name in TABLES}
    found, _ = birthday_rows(tables)
    days = birthday_days(found, watch, fields) if watch is not None else ()
    for name, rows in editions(found).items():
        for row in rows:
            if not shows(name, row) or permanent(row, fields[name]):
                continue
            watched = watch is not None and (
                (name == SPLASH and character(row) == watch)
                or day_of(row[fields[name]['start_date']]) in days)
            before = tuple(row[fields[name][field]] for field in ('start_date', 'end_date'))
            for field in ('start_date', 'end_date'):
                moved = move(row[fields[name][field]], year)
                if moved is not None:
                    row[fields[name][field]] = moved
            if not watched:
                if before != tuple(row[fields[name][field]] for field in ('start_date', 'end_date')):
                    changes.append((name, row[0], f'birthday-window-{year}'))
                continue
            start = fresh()
            row[fields[name]['start_date']] = msgpack.Timestamp(start, 0)
            row[fields[name]['end_date']] = msgpack.Timestamp(start + 86400, 0)
            changes.append((name, row[0], f'birthday-today-{watch}'))
    return found
