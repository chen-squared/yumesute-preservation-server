"""演劇リーグ (weekly) and 演劇リーグ・トリプルキャスト (fortnightly).

Official, straight from the recovered master and the protocol:

  * the window shape and phase (see league_schedule) and the song / bonus-character
    pairing inside each window,
  * the class ladder in LeagueClassGroupMaster (Rookie .. DaiStar) with its
    up_user_amount / keep_user_amount / is_force_down_not_playing_league rules and the
    four reward packages per class (up / keep / down / first-achieve),
  * the per-play rewards: LeaguePlayRewardPackageMaster is keyed by window id, and all
    202 archived ids are league or triple cast windows,
  * the group-rank and all-class-global rank reward bands,
  * the entity surface the client expects: LeagueBasic / LeagueGroup / LeagueGroupMember
    / LeagueHighScoreParty(+Slot) / LeagueHistory and the TripleCast twins, plus the
    TopMenuInformation / GroupRanking / GlobalRanking results and the
    LeagueReceiveResults the ladder pays out through GET /api/Leagues.

Not official - the one invented part - is the opponents. An official group is 100 real
players and the recovery holds a single account, so the field is generated locally in
preservation_league_rival and never written into any official table. Rival scores are
anchored to this account's own best result, the only score scale the archive implies,
which keeps the official rank-based promotion rule meaningful: about half the generated
field sits above your best, so promotion asks you to beat your best and holding a class
asks for more than that. Generated rivals are labelled so they cannot be mistaken for
recovered players.

One rule the archive only implies is stated in one place: a ladder run costs no stamina.
All 37 archived triple cast windows and 117 of the 165 league windows use a 10000-series
song priced at 0 stamina, and ladder_stamina_cost is where both live paths read that.
"""
import json
import random
import re
import time
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, Request
from db.user import get_partys, get_party_slots
from helpers.cache import cache
from helpers.live_result import play_totals
from helpers.msgpack import fault, read_request, respond
from live_parity import _character_base_status, _star_rank_bonus
from models import (
    LeaguePartyAndRankingResult,
    LeaguePartySlotResult,
    LeagueReceiveResults,
    LeagueResult,
    LeagueTopMenuInformationResult,
    StartTripleCastLivePayload,
    Status,
    TripleCastPartyAndRankingResult,
    TripleCastPartyResult,
)
from models.enums import LeagueClassChangeTypes, LeagueClassTypes

import league_schedule as schedule
from gameplay import Rejected, master, mission_progress, transaction

router = APIRouter()
ROOT = Path(__file__).resolve().parents[1]
MASTERDATA = ROOT / 'vendor' / 'server-of-dreams' / '_data' / 'masterdata'

# An official group holds 100 players; the hundredth of them is this account.
RIVAL_COUNT = 99
# The generated field has its own table: it must never reach an official entity table,
# so nothing here writes a LeagueGroupMember / TripleCastGroupMember row for a rival.
RIVAL_TABLE = "preservation_league_rival"
# The label is deliberate: a generated rival must never read like a recovered player.
RIVAL_LABEL = 'ローカル対抗'
# Floor for the field when the account has never posted a result in the ladder at all.
MIN_REFERENCE = 300_000
# Only the head of a global ranking page is ever rendered.
GLOBAL_PAGE = 100
# LeagueClassTypes 1..7: Rookie, Hope, Cast, Elite, Veteran, Star, DaiStar.
CLASSES = list(range(1, 8))
TOP_CLASS = CLASSES[-1]
STAR_CLASS = CLASSES[-2]

SPEC = {
    'league': {
        'schedule': 'league',
        'basic': 'LeagueBasic',
        'basic_key': 'myProperty',
        'season_column': 'lastJoinedLeagueSeasonMasterId',
        'group': 'LeagueGroup',
        'member': 'LeagueGroupMember',
        'member_group': 'leagueGroupId',
        'history': 'LeagueHistory',
        'party': 'LeagueHighScoreParty',
        'party_slot': 'LeagueHighScorePartySlot',
        'party_slot_parent': 'leagueHighScorePartyId',
        'window': 'leagueMasterId',
        'all_class_table': 'LeagueAllClassGlobalRankingRewardThingMaster',
        'all_class_window': 'league_master_id',
    },
    'triple_cast': {
        'schedule': 'triple_cast',
        'basic': 'TripleCastBasic',
        'basic_key': 'id',
        'season_column': 'lastJoinedTripleCastSeasonMasterId',
        'group': 'TripleCastGroup',
        'member': 'TripleCastGroupMember',
        'member_group': 'tripleCastGroupId',
        'history': 'TripleCastHistory',
        'party': 'TripleCastHighScoreParty',
        'party_slot': 'TripleCastHighScorePartySlot',
        'party_slot_parent': 'tripleCastHighScorePartyId',
        'window': 'tripleCastMasterId',
        'all_class_table': 'TripleCastAllClassGlobalRankingRewardThingMaster',
        'all_class_window': 'triple_cast_master_id',
    },
}


def _now():
    return int(time.time_ns() // 1_000_000_000)


def _table(entity):
    return re.sub(r'(?<!^)(?=[A-Z])', '_', entity).lower()


@lru_cache(maxsize=None)
def raw_master(table):
    """Master rows the generated pydantic models cannot represent.

    The archive keeps the all-class-global reward *packages* (id, window id, rewards,
    min_rank, max_rank, title) in tables named ``*ThingMaster``, whose generated model
    only carries the three thing columns - so those two tables are read from source.
    """
    return json.loads((MASTERDATA / f'{table}.json').read_text(encoding='utf-8'))


# --- ladder ---------------------------------------------------------------------------


def ladder(window, class_type):
    """The LeagueClassMaster row for one class of this window's ladder."""
    group = master('league_class_group_master', window.class_group)
    return next((c for c in (group.classes or []) if int(c.class_type) == int(class_type)), None)


def _things(package):
    """A package's rewards as ``(thing_type, thing_id, quantity)`` triples.

    Packages come from two places: the master cache (pydantic models) and the two tables
    that are read from source JSON because their generated model cannot hold them (plain
    dicts), so both shapes are accepted here.
    """
    rewards = package.rewards if hasattr(package, 'rewards') else package['rewards']
    out = []
    for thing in rewards or []:
        read = thing.get if isinstance(thing, dict) else lambda field, thing=thing: getattr(thing, field)
        out.append((int(read('thing_type')), int(read('thing_id')), int(read('thing_quantity'))))
    return out


def _band(package, rank):
    """Rank band membership.

    The archive stores these two columns the other way round from their names - the
    group-rank bands read ``(min_rank 30, max_rank 11)`` for ranks 11..30, and the last
    all-class band reads ``(min_rank 9999999, max_rank 101)`` - so the pair is matched
    as the unordered range it plainly is.
    """
    low, high = sorted((int(package['min_rank'] if isinstance(package, dict) else package.min_rank),
                        int(package['max_rank'] if isinstance(package, dict) else package.max_rank)))
    return low <= int(rank) <= high


def group_rank_package(kind, class_type, rank):
    table = ('league_group_ranking_reward_package_master' if kind == 'league'
             else 'triple_cast_group_ranking_reward_package_master')
    return next(
        (p for p in getattr(cache, table)
         if int(p.class_type) == int(class_type) and _band(p, rank)),
        None,
    )


def all_class_package(kind, window_id, rank):
    """The per-window all-class-global reward band.

    Only archived windows have rows here - these are per-window trophy keepsakes - so a
    generated window pays the class package and the group-rank band and nothing else.
    """
    spec = SPEC[kind]
    return next(
        (p for p in raw_master(spec['all_class_table'])
         if int(p[spec['all_class_window']]) == int(window_id) and _band(p, rank)),
        None,
    )


def play_reward_things(window):
    package = master('league_play_reward_package_master', window.id)
    return _things(package) if package else []


def master_reward_package(package_id):
    """An up / keep / down / first-achieve package from LeagueRewardPackageMaster."""
    return master('league_reward_package_master', package_id) if package_id else None


def int_change(name):
    """The numeric value of a LeagueClassChangeTypes member (None, Up, Keep, Down)."""
    return int(getattr(LeagueClassChangeTypes, name))


def ladder_stamina_cost(music):
    """What a ladder run costs in stamina: nothing, for either ladder.

    Both 演劇リーグ and トリプルキャスト are score-farming modes meant to be replayed at
    will, and the archived master prices their songs that way: all 37 triple cast windows
    and 117 of the 165 league windows use a 10000-series song whose stamina_consumption is
    0. The league payload does carry ``use_stamina`` and ``StartTripleCastLivePayload``
    carries no stamina field at all, so the rule is kept here once instead of in either
    payload, and a ladder run never spends stamina whichever way it arrives.
    """
    return 0


_FREE_RUN = ContextVar('free_ladder_run', default=False)
_upstream_stamina_cost = None


def begin_free_run():
    """Mark the live start about to run as a ladder run (returns a reset token)."""
    return _FREE_RUN.set(True)


def end_free_run(token) -> None:
    _FREE_RUN.reset(token)


def _stamina_cost(live_master_id, ratio):
    """The price ``Lives/Start`` charges: nothing while a ladder run is in flight.

    The upstream route re-reads its own payload, so the free rule cannot be applied to
    the payload parsed here; pricing the run instead also keeps ``active_live`` recording
    that no stamina was spent, rather than taking stamina and handing it back afterwards.
    """
    if _FREE_RUN.get():
        return 0
    return _upstream_stamina_cost(live_master_id, ratio)


# --- storage --------------------------------------------------------------------------


async def write(s, entity, key, values):
    """Insert or update one row by its natural key, and mark it for the client.

    Entity and column names come from SPEC in this module, never from a request.
    """
    table = _table(entity)
    where = ' AND '.join(f'"{name}"=${index + 2}' for index, name in enumerate(key))
    row = await s.conn.conn.fetchrow(f'SELECT * FROM "{table}" WHERE "userId"=$1 AND {where}',
                                     s.uid, *key.values())
    if row and values:
        sets = ', '.join(f'"{name}"=${index + 2}' for index, name in enumerate(values))
        await s.conn.conn.execute(
            f'UPDATE "{table}" SET {sets} WHERE "userId"=$1 AND "rowId"=${len(values) + 2}',
            s.uid, *values.values(), row['rowId'],
        )
    elif not row:
        columns = {**key, **values}
        names = ', '.join(f'"{name}"' for name in columns)
        marks = ', '.join(f'${index + 2}' for index in range(len(columns)))
        await s.conn.conn.execute(
            f'INSERT INTO "{table}" ("userId", {names}) VALUES ($1, {marks})',
            s.uid, *columns.values(),
        )
    fresh = dict(await s.conn.conn.fetchrow(f'SELECT * FROM "{table}" WHERE "userId"=$1 AND {where}',
                                            s.uid, *key.values()))
    s.tables.pop(entity, None)
    s.dirty[(entity, fresh[next(iter(key))])] = fresh
    return fresh


# --- account state --------------------------------------------------------------------


async def basic(s, kind):
    spec = SPEC[kind]
    row = await s.one(spec['basic'])
    if row:
        return row
    values = {
        'starEnrollCount': 0,
        'daiStarEnrollCount': 0,
        'currentClassType': CLASSES[0],
        'bestClassType': CLASSES[0],
        spec['season_column']: None,
    }
    if kind == 'league':
        key = {'myProperty': 0}
    else:
        key = {'id': max([r['id'] for r in await s.rows(spec['basic'])] + [0]) + 1}
        values.update({'partyOrder1': 0, 'partyOrder2': 1, 'partyOrder3': 2})
    return await write(s, spec['basic'], key, values)


async def group(s, kind, window):
    return await s.one(SPEC[kind]['group'], **{SPEC[kind]['window']: window.id})


async def member(s, kind, window):
    return await s.one(SPEC[kind]['member'], **{SPEC[kind]['window']: window.id})


async def history(s, kind, window):
    return await s.one(SPEC[kind]['history'], **{SPEC[kind]['window']: window.id})


async def join(s, kind, window):
    """Enter a window: group row, member row, history row.

    The protocol has no join call (ITheaterLeagueModel only offers initialize,
    SetDifficulty and the ranking calls), so joining is implicit in playing - which is
    also what the server-side debug op Debug_JoinOnCreateLeagueGroupMember implies.
    """
    spec = SPEC[kind]
    existing = await group(s, kind, window)
    if existing:
        return existing
    mine = await basic(s, kind)
    row = await write(s, spec['group'], {spec['window']: window.id},
                      {'classType': mine['currentClassType'], 'classOrder': 1})
    await write(s, spec['member'], {spec['window']: window.id},
                {spec['member_group']: row['rowId'], 'bestScore': None})
    await write(
        s, spec['history'],
        {spec['window']: window.id},
        {'classType': mine['currentClassType'], 'historyCount': 0, 'isSendedReward': False,
         'isPlayed': False, 'classChangeType': int(LeagueClassChangeTypes.None_),
         'groupRank': 0, 'globalRank': 0, 'allClassGlobalRank': 0},
    )
    if mine[spec['season_column']] != window.season:
        await write(s, spec['basic'], {spec['basic_key']: mine[spec['basic_key']]},
                    {spec['season_column']: window.season})
    return row


# --- the generated field --------------------------------------------------------------


async def reference(s, kind, window):
    """The score the field is anchored to: this account's best in the ladder."""
    spec = SPEC[kind]
    best = 0
    for row in await s.rows(spec['member']):
        best = max(best, int(row['bestScore'] or 0))
    for row in await s.rows(spec['party']):
        best = max(best, int(row['highScore'] or 0))
    current = await member(s, kind, window)
    if current:
        best = max(best, int(current['bestScore'] or 0))
    return max(best, MIN_REFERENCE)


@lru_cache(maxsize=8)
def card_pool():
    characters = [(c.id_, int(c.rarity), int(c.attribute)) for c in cache.character_master]
    return characters, [p.id_ for p in cache.poster_master], [a.id_ for a in cache.accessory_master]


def rival_party(rng, strength):
    """Five official cards at a level that matches the rival's standing (display only)."""
    characters, posters, accessories = card_pool()
    level = max(1, min(120, int(20 + 90 * strength)))
    return [
        {
            'character_master_id': card[0],
            'character_rarity': card[1],
            'attribute': card[2],
            'character_level': max(1, level - rng.randrange(0, 20)),
            'poster_master_id': rng.choice(posters),
            'poster_level': max(1, level - rng.randrange(0, 30)),
            'accessory_master_id': rng.choice(accessories),
            'accessory_level': rng.randrange(1, 11),
            'status': 2600 + int(strength * 2400) + rng.randrange(0, 400),
        }
        for card in rng.sample(characters, 5)
    ]


def rival_score(base, ordinal, rng):
    """One rival's score: a smooth curve from 1.30x down to 0.55x of the account's best.

    With 99 rivals around you, the official cut lines land where the ladder intends:
    rising out of Hope needs a result better than your own best, holding Cast needs a
    comfortable margin over it.
    """
    span = (ordinal - 1) / (RIVAL_COUNT - 1)
    factor = (1.30 - 0.75 * span) * rng.uniform(0.97, 1.03)
    return max(1, int(base * factor)), factor


def party_ability(party):
    """Acting ability shown for a party: the sum of its cards' three stats."""
    return sum(int(card.get('status') or 0) for card in party or [])


async def rivals(s, kind, window):
    """The 99 generated opponents of this window, one field per class."""
    rows = await s.conn.conn.fetch(
        f'SELECT * FROM {RIVAL_TABLE} WHERE "userId"=$1 AND kind=$2 AND "masterId"=$3',
        s.uid, kind, window.id,
    )
    if rows:
        return [{**dict(row), 'actingAbility': party_ability(row['party'])} for row in rows]
    base = await reference(s, kind, window)
    created = []
    for class_type in CLASSES:
        rng = random.Random(f'{kind}:{window.id}:{class_type}')
        for ordinal in range(1, RIVAL_COUNT + 1):
            score, factor = rival_score(base, ordinal, rng)
            party = rival_party(rng, max(0.0, min(1.3, factor)))
            rival = {
                'classType': class_type,
                'score': score,
                'party': party,
                'userHash': f'lp{window.id}{class_type:02d}{ordinal:02d}',
                'name': f'{RIVAL_LABEL} {class_type}-{ordinal:02d}',
                'actingAbility': party_ability(party),
            }
            await s.conn.conn.execute(
                f'INSERT INTO {RIVAL_TABLE} '
                '("userId", kind, "masterId", "classType", ordinal, "userHash", name, score, party) '
                'VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)',
                s.uid, kind, window.id, class_type, ordinal, rival['userHash'], rival['name'],
                score, rival['party'],
            )
            created.append(rival)
    return created


async def standings(s, kind, window):
    """Where this account sits: in its class group, and across every class."""
    mine = await basic(s, kind)
    field = await rivals(s, kind, window)
    entry = await member(s, kind, window)
    score = int((entry or {}).get('bestScore') or 0)
    own = mine['currentClassType']
    same = sorted((r['score'] for r in field if r['classType'] == own), reverse=True)
    everyone = sorted((r['score'] for r in field), reverse=True)
    group_rank = 1 + sum(1 for value in same if value > score)
    global_rank = 1 + sum(1 for value in everyone if value > score)
    borders, cls = {}, ladder(window, own)
    if cls:
        for name, amount in (('up', cls.up_user_amount), ('keep', cls.keep_user_amount)):
            if amount and 0 < int(amount) <= len(same):
                borders[f'{name}_border_score'] = same[int(amount) - 1]
                borders[f'{name}_border_ranking'] = int(amount)
    return {
        'group_rank': group_rank,
        'global_rank': global_rank,
        'score': score,
        'group_size': len(same) + 1,
        'class_type': own,
        'borders': borders,
        'field': field,
    }


def slot_result(position, card):
    """One card of a ranking party, from our own slot rows or from a generated rival's."""
    status = int(card.get('status') or 0)
    return LeaguePartySlotResult(
        slot_position=position,
        character_master_id=int(card['character_master_id']),
        attribute=int(card.get('attribute') or 1),
        character_rarity=int(card.get('character_rarity') or 1),
        character_level=int(card.get('character_level') or 1),
        poster_master_id=card.get('poster_master_id') or None,
        poster_level=card.get('poster_level') or None,
        accessory_master_id=card.get('accessory_master_id') or None,
        accessory_level=card.get('accessory_level') or None,
        current_status=Status(concentration=status, expression=status, vocal=status),
        position=position,
        character_talent_stage=int(card.get('character_talent_stage') or 0),
        character_awakening_phase=int(card.get('character_awakening_phase') or 0),
        character_display_awakening_status=bool(card.get('character_display_awakening_status')),
    )


# --- scoring --------------------------------------------------------------------------


async def party_cards(s, party_id, triple=False):
    """The cards of the party that just played, in the shape the ladder stores.

    ``current_status`` is the card's intrinsic stat (level curve + awakening + star
    rank) - the same ``_character_base_status`` the live scorer uses before poster and
    accessory effects. The ladder only shows these numbers, and computing the full
    effect pipeline again here would duplicate the live scorer.
    """
    slots = [r for r in await s.rows('TripleCastPartySlot' if triple else 'PartySlot')
             if r['partyId'] == party_id]
    characters = {r['id']: r for r in await s.rows('Character')}
    bases = {r['id']: r for r in await s.rows('CharacterBase')}
    posters = {r['id']: r for r in await s.rows('Poster')}
    accessories = {r['id']: r for r in await s.rows('Accessory')}
    cards = []
    for slot in sorted(slots, key=lambda r: r['position']):
        character = characters.get(slot['characterId'])
        if not character:
            continue
        master_row = master('character_master', character['characterMasterId'])
        base = bases.get(character['characterBaseId'])
        intrinsic = int(character['awakeningPhase']) * 10 + _star_rank_bonus(
            int(base['starRank']) if base else 0)
        vocal, expression, concentration = _character_base_status(
            master_row, int(character['level']), intrinsic=intrinsic)
        poster = posters.get(slot['posterId']) or {}
        cards.append({
            'character_master_id': character['characterMasterId'],
            'character_level': int(character['level']),
            'character_rarity': int(getattr(master_row, 'rarity', 1) or 1),
            'attribute': int(getattr(master_row, 'attribute', 1) or 1),
            'character_talent_stage': int(character['talentStage']),
            'character_awakening_phase': int(character['awakeningPhase']),
            'character_display_awakening_status': bool(character['displayAwakeningStatus']),
            'poster_master_id': poster.get('posterMasterId'),
            'poster_level': int(poster.get('level') or 0) or None,
            'poster_breakthrough_phase': int(poster.get('breakthroughPhase') or 0),
            'accessory_master_id': (accessories.get(slot['accessoryId']) or {}).get('accessoryMasterId'),
            'accessory_level': int((accessories.get(slot['accessoryId']) or {}).get('level') or 0) or None,
            'current_status': {'concentration': concentration, 'expression': expression,
                               'vocal': vocal},
        })
    return cards


def acting_ability(cards):
    return sum(sum(card['current_status'].values()) for card in cards)


def party_id_for(kind, window, order=1):
    """Deterministic ladder party ids: one per window (league), per order (triple cast)."""
    return window.id if kind == 'league' else window.id * 10 + order


async def post(s, kind, window, score, cards, difficulty, leader_position, order=1,
               total=None):
    """Store one ladder result: best score, the party that got it, the played flag.

    The group member row keeps the window's best total. A triple cast run posts three
    parties against that one total, so each order also keeps its own best in its own
    high-score row, and only a better run replaces the party shown for the window.
    """
    spec = SPEC[kind]
    entry = await member(s, kind, window)
    before = int(entry['bestScore'] or 0)
    best = max(before, int(total if total is not None else score))
    await write(s, spec['member'], {spec['window']: window.id}, {'bestScore': best})
    row = await history(s, kind, window)
    if row and not row['isPlayed']:
        await write(s, spec['history'], {spec['window']: window.id}, {'isPlayed': True})
    ident = party_id_for(kind, window, order)
    key = {'id': ident, spec['window']: window.id}
    if kind != 'league':
        key['order'] = int(order)
    existing = await s.one(spec['party'], **key)
    if cards and int(score) > int(existing['highScore'] if existing else 0):
        await write_party(s, kind, window, ident, cards, score, difficulty, leader_position, order)
    return before, best


async def write_party(s, kind, window, ident, cards, score, difficulty, leader_position, order):
    """Store the party that set a window's best score, in both places it appears."""
    spec = SPEC[kind]
    mine = await basic(s, kind)
    values = {
        'highScore': int(score),
        'difficulty': int(difficulty),
        'actingAbility': acting_ability(cards),
        'leaderPosition': int(leader_position),
        # The party row keeps its own copy of the slots; the protocol carries both, and
        # the official upsert writes both too.
        'slots': [
            {
                'id': ident * 100 + position,
                spec['party_slot_parent']: ident,
                **slot_columns(position, ident, card, spec),
            }
            for position, card in enumerate(cards, start=1)
        ],
    }
    key = {'id': ident, spec['window']: window.id}
    if kind == 'league':
        # league_high_score_party also carries the class, the song and the group; the
        # triple cast row does not.
        values.update({
            'classType': mine['currentClassType'],
            'musicMasterId': window.music,
            'leagueGroupId': (await group(s, kind, window) or {}).get('rowId', 0),
            'userName': (await s.one('UserProfile') or {}).get('name'),
        })
    else:
        key['order'] = int(order)
    await write(s, spec['party'], key, values)
    for position, card in enumerate(cards, start=1):
        await write(s, spec['party_slot'],
                    {'id': ident * 100 + position, spec['party_slot_parent']: ident},
                    slot_columns(position, ident, card, spec))


def slot_columns(position, ident, card, spec):
    """One slot row - and the same shape inside the party row's inline slots column."""
    return {
        'position': position,
        'characterMasterId': card['character_master_id'],
        'characterLevel': card['character_level'],
        'posterMasterId': card.get('poster_master_id'),
        'posterLevel': card.get('poster_level'),
        'posterBreakthroughPhase': card.get('poster_breakthrough_phase', 0),
        'accessoryMasterId': card.get('accessory_master_id'),
        'accessoryLevel': card.get('accessory_level'),
        'currentStatus': card.get('current_status') or {},
        'characterTalentStage': card['character_talent_stage'],
        'characterAwakeningPhase': card['character_awakening_phase'],
        'characterDisplayAwakeningStatus': card['character_display_awakening_status'],
    }


async def result_fields(s, kind, window, board):
    borders = board['borders']
    return LeagueResult(
        before_group_ranking=None,
        after_group_ranking=board['group_rank'],
        lowest_up_border_score=borders.get('up_border_score'),
        lowest_keep_border_score=borders.get('keep_border_score'),
        enrolled_group_number=1,
        is_in_counting=window.phase(_now()) == 'counting',
    )


# --- settlement and payout ------------------------------------------------------------


def recorded_outcome(row):
    """A history row the official server already graded keeps its official numbers.

    The recovered save has exactly this shape: ranks and a class change recorded, but
    ``isSendedReward`` still false because the service stopped before the payout.
    """
    if row and row['groupRank'] and row['classChangeType']:
        return {'group_rank': row['groupRank'], 'global_rank': row['globalRank'],
                'all_class_global_rank': row['allClassGlobalRank'], 'class_type': row['classType'],
                'change': row['classChangeType'], 'official': True}
    return None


def change_for(cls, group_rank, played):
    """The official class rule.

    The top ``up_user_amount`` ranks of the group rise; a class falls if it drops past
    the keep band, or - from Cast upward, where ``is_force_down_not_playing_league`` is
    set - if the player never played the window at all.
    """
    if cls is None:
        return int(LeagueClassChangeTypes.Keep)
    up = int(cls.up_user_amount or 0)
    if up and group_rank <= up:
        return int(LeagueClassChangeTypes.Up)
    if cls.is_force_down_not_playing_league and not played:
        return int(LeagueClassChangeTypes.Down)
    if cls.keep_user_amount and group_rank > up + int(cls.keep_user_amount):
        return int(LeagueClassChangeTypes.Down)
    return int(LeagueClassChangeTypes.Keep)


async def settle(s, kind, window):
    """Ranks and the class change for a closed window."""
    row = await history(s, kind, window)
    if not row:
        return None
    official = recorded_outcome(row)
    if official:
        return official
    board = await standings(s, kind, window)
    return {
        'group_rank': board['group_rank'],
        'global_rank': board['global_rank'],
        'all_class_global_rank': board['global_rank'],
        'class_type': board['class_type'],
        'change': change_for(ladder(window, board['class_type']), board['group_rank'],
                             bool(row['isPlayed'])),
        'official': False,
    }


def moved(class_type, change):
    if change == int(LeagueClassChangeTypes.Up):
        return min(TOP_CLASS, class_type + 1)
    if change == int(LeagueClassChangeTypes.Down):
        return max(CLASSES[0], class_type - 1)
    return class_type


async def pay(s, kind, window, outcome, into):
    """Grant one window's rewards into ``into`` and mark its history row paid."""
    spec = SPEC[kind]
    cls = ladder(window, outcome['class_type'])
    mine = await basic(s, kind)
    into.class_reward_type = LeagueClassChangeTypes(outcome['change'])

    if cls is not None:
        package = master_reward_package({
            int(LeagueClassChangeTypes.Up): cls.up_reward_package_master_id,
            int(LeagueClassChangeTypes.Keep): cls.keep_reward_package_master_id,
            int(LeagueClassChangeTypes.Down): cls.down_reward_package_master_id,
        }.get(outcome['change']))
        if package:
            into.class_reward_received_things = (into.class_reward_received_things or []) + \
                await s.grant(_things(package))

    band = group_rank_package(kind, outcome['class_type'], outcome['group_rank'])
    if band:
        into.group_rank_received_things = (into.group_rank_received_things or []) + \
            await s.grant(_things(band))
    band = all_class_package(kind, window.id, outcome['all_class_global_rank'])
    if band:
        into.all_class_global_rank_received_things = (into.all_class_global_rank_received_things or []) + \
            await s.grant(_things(band))

    after = moved(outcome['class_type'], outcome['change'])
    first_time = after > int(mine['bestClassType'])
    if first_time and cls is not None and cls.first_achieve_reward_package_master_id:
        package = master_reward_package(cls.first_achieve_reward_package_master_id)
        if package:
            into.first_achieve_received_things = (into.first_achieve_received_things or []) + \
                await s.grant(_things(package))
            into.is_first_time_enrolled_class = True

    row = await history(s, kind, window)
    await write(
        s, spec['history'],
        {spec['window']: window.id},
        {'isSendedReward': True, 'classChangeType': outcome['change'],
         'groupRank': outcome['group_rank'], 'globalRank': outcome['global_rank'],
         'allClassGlobalRank': outcome['all_class_global_rank'], 'classType': outcome['class_type'],
         'historyCount': int(row['historyCount']) + 1},
    )
    updates = {'currentClassType': after,
               'bestClassType': max(int(mine['bestClassType']), int(after))}
    if after == TOP_CLASS:
        updates['daiStarEnrollCount'] = int(mine['daiStarEnrollCount']) + 1
    if after == STAR_CLASS:
        updates['starEnrollCount'] = int(mine['starEnrollCount']) + 1
    await write(s, spec['basic'], {spec['basic_key']: mine[spec['basic_key']]}, updates)
    profile = await s.one('UserProfile')
    if profile and profile.get('leagueClass') != after:
        await s.update('UserProfile', profile, leagueClass=after)
    return after


async def push(s, kind, only_official=False):
    """Pay every closed window that has not been paid yet, oldest first.

    The protocol returns one LeagueReceiveResults, so several pending windows are
    merged into it; each window still grades on its own.
    """
    last_official = schedule.anchor(SPEC[kind]['schedule']).id
    merged = LeagueReceiveResults()
    for window in sorted(schedule.closed(SPEC[kind]['schedule']), key=lambda w: w.display_end):
        row = await history(s, kind, window)
        if not row or row['isSendedReward']:
            continue
        if only_official and window.id > last_official:
            continue
        outcome = await settle(s, kind, window)
        if outcome is not None:
            await pay(s, kind, window, outcome, merged)
    return merged


# --- routes ---------------------------------------------------------------------------


async def _top_menu(request, kind):
    window = schedule.live(SPEC[kind]['schedule'])
    if window is None:
        return respond(LeagueTopMenuInformationResult())
    async with transaction(request) as s:
        joined = await group(s, kind, window)
        board = await standings(s, kind, window) if joined else None
    borders = (board or {}).get('borders', {})
    return respond(LeagueTopMenuInformationResult(
        display_start_at=schedule.iso(window.display_start),
        counting_start_at=schedule.iso(window.counting_start),
        display_end_at=schedule.iso(window.display_end),
        music_master_id=window.music,
        league_group_ranking=(board or {}).get('group_rank', 0),
        up_class_border_score=borders.get('up_border_score'),
        up_class_border_ranking=borders.get('up_border_ranking'),
        keep_class_border_score=borders.get('keep_border_score'),
        keep_class_border_ranking=borders.get('keep_border_ranking'),
        group_lowest_rank=(board or {}).get('group_size'),
        is_participated=bool(joined),
        enrolled_group_number=1 if joined else None,
    ))


@router.post('/api/Leagues/TopMenuInformation', name='Leagues_GetTopMenuInformation')
async def league_top_menu(request: Request):
    return await _top_menu(request, 'league')


@router.post('/api/TripleCast/TopMenuInformation', name='Leagues_GetTripleCastTopMenuInformation')
async def triple_cast_top_menu(request: Request):
    return await _top_menu(request, 'triple_cast')


def _row(kind, window, rank, class_type, name, user_id, score, acting, slots):
    if kind == 'league':
        return LeaguePartyAndRankingResult(
            rank=rank, user_id=user_id, best_score=int(score), league_master_id=window.id,
            class_type=LeagueClassTypes(class_type), user_name=name, acting_ability=int(acting),
            slots=slots, difficulty=int(window.difficulty),
        )
    return TripleCastPartyAndRankingResult(
        rank=rank, user_id=user_id, best_score=int(score), triple_cast_master_id=window.id,
        class_type=LeagueClassTypes(class_type), difficulty=int(window.difficulty), user_name=name,
        parties=[TripleCastPartyResult(order=order, score=int(score), slots=slots)
                 for order in (1, 2, 3)],
    )


async def _ranking(request, kind, window_id, global_page=False):
    window = schedule.find(SPEC[kind]['schedule'], window_id)
    if window is None:
        return respond([])
    spec = SPEC[kind]
    async with transaction(request) as s:
        board = await standings(s, kind, window)
        own = await s.one(spec['party'], **{spec['window']: window.id})
        cards = []
        if own:
            cards = sorted((r for r in await s.rows(spec['party_slot'])
                            if r[spec['party_slot_parent']] == own['id']), key=lambda r: r['position'])
        user = await s.one('User')
        profile = await s.one('UserProfile') or {}
    own_slots = [
        slot_result(row['position'], {
            'character_master_id': row['characterMasterId'],
            'character_level': row['characterLevel'],
            'poster_master_id': row['posterMasterId'], 'poster_level': row['posterLevel'],
            'accessory_master_id': row['accessoryMasterId'], 'accessory_level': row['accessoryLevel'],
            'character_talent_stage': row['characterTalentStage'],
            'character_awakening_phase': row['characterAwakeningPhase'],
            'character_display_awakening_status': row['characterDisplayAwakeningStatus'],
            'status': int((row['currentStatus'] or {}).get('vocal') or 0),
        })
        for row in cards
    ]
    field = [r for r in board['field'] if global_page or r['classType'] == board['class_type']]
    rows = sorted(field, key=lambda r: -r['score'])[:GLOBAL_PAGE if global_page else len(field)]
    out = [
        _row(kind, window, rank, rival['classType'], rival['name'], rival['userHash'], rival['score'],
             rival['actingAbility'],
             [slot_result(position, card) for position, card in enumerate(rival['party'], start=1)])
        for rank, rival in enumerate(rows, start=1)
    ]
    out.append(_row(
        kind, window, board['global_rank'] if global_page else board['group_rank'],
        board['class_type'], profile.get('name'), (user or {}).get('hashUserId'), board['score'],
        int((own or {}).get('actingAbility') or 0), own_slots,
    ))
    return respond(out)


@router.post('/api/Leagues/GroupRanking/{leagueMasterId}/{leagueGroupId}', name='Leagues_GetGroupRanking')
async def league_group_ranking(request: Request, leagueMasterId: int, leagueGroupId: int):
    return await _ranking(request, 'league', leagueMasterId)


@router.post('/api/Leagues/GlobalRanking/{leagueMasterId}', name='Leagues_GetGlobalRanking')
async def league_global_ranking(request: Request, leagueMasterId: int):
    return await _ranking(request, 'league', leagueMasterId, global_page=True)


@router.post('/api/TripleCast/GroupRanking/{tripleCastMasterId}', name='Leagues_GetTripleCastGroupRanking')
async def triple_cast_group_ranking(request: Request, tripleCastMasterId: int):
    return await _ranking(request, 'triple_cast', tripleCastMasterId)


@router.post('/api/TripleCast/GlobalRanking/{tripleCastMasterId}', name='Leagues_GetTripleCastGlobalRanking')
async def triple_cast_global_ranking(request: Request, tripleCastMasterId: int):
    return await _ranking(request, 'triple_cast', tripleCastMasterId, global_page=True)


@router.get('/api/Leagues', name='Leagues_SendThingLeagueRewards')
async def send_league_rewards(request: Request):
    async with transaction(request) as s:
        merged = await push(s, 'league')
        present = s.present()
    return respond(merged, present=present)


@router.get('/api/TripleCast', name='Leagues_SendTripleCastThingLeagueRewards')
async def send_triple_cast_rewards(request: Request):
    async with transaction(request) as s:
        merged = await push(s, 'triple_cast')
        present = s.present()
    return respond(merged, present=present)


# --- live integration -----------------------------------------------------------------


def window_for(kind, identifier=None):
    """The window a ladder run belongs to: the one the client named, else the live one."""
    if identifier:
        return schedule.find(SPEC[kind]['schedule'], identifier)
    return schedule.live(SPEC[kind]['schedule'])


async def begin(s, kind, window):
    """Join before the live starts, so the score has somewhere to land."""
    if window is None:
        raise Rejected('No league window is open')
    if window.phase(_now()) == 'upcoming':
        raise Rejected('The league has not started yet')
    await join(s, kind, window)
    return window


async def score_league(s, kind, window, cards, score, difficulty, leader_position, result,
                       order=None, total=None):
    """Post a ladder run: best score, the party that got it, the play reward, the mission."""
    before, best = await post(s, kind, window, score, cards, difficulty, leader_position,
                              order=order or 1, total=total)
    board = await standings(s, kind, window)
    result.league_result = await result_fields(s, kind, window, board)
    things = play_reward_things(window)
    if things:
        result.league_rewards = await s.grant(things)
    if kind == 'league':
        # 200100 「演劇リーグを[:param]回プレイしよう」 - the official weekly mission.
        await mission_progress(s, 200100)
    return before, best


class _TripleCastView:
    """Presents triple cast parties to ``helpers.live``, which reads the party tables.

    A triple cast party lives in triple_cast_party(+slot) rather than party(+slot), but
    the live unit has to be built exactly the same way. Instead of duplicating the live
    scoring helpers, the two queries they issue are answered from the triple cast rows.
    """

    def __init__(self, conn, slots, parties):
        self.conn, self.slots, self.parties = conn, slots, parties
        self._slots_sql = get_party_slots(0).sql
        self._parties_sql = get_partys(0).sql

    def __getattr__(self, name):
        return getattr(self.conn, name)

    async def fetch(self, query):
        sql = getattr(query, 'sql', None)
        if sql == self._slots_sql:
            return [
                SimpleNamespace(
                    id=row['rowId'], partyId=row['partyId'], position=row['position'],
                    characterId=row['characterId'], posterId=row['posterId'],
                    accessoryId=row['accessoryId'],
                    bonusAbilityEnableFlags=row['bonusAbilityEnableFlags'],
                )
                for row in self.slots
            ]
        if sql == self._parties_sql:
            return [
                SimpleNamespace(id=row['id'], name=row['name'], order=row['order'],
                                leaderPosition=row['leaderPosition'])
                for row in self.parties
            ]
        return await self.conn.fetch(query)


@router.post('/api/Lives/StartTripleCastLive', name='Lives_StartTripleCastLive')
async def start_triple_cast_live(request: Request):
    """Open a triple cast run: three parties on one song, each with its own bonus set.

    The payload names only the window (mLiveId + mTripleCastId), so the three parties are
    the ones TripleCastBasic selects through partyOrder1/2/3, and order n plays against
    sense_notation_master_id n. No stamina is charged: see ladder_stamina_cost.
    """
    from helpers.live import build_live_time_event, build_live_unit
    from db.user import create_active_live, delete_active_lives
    from models import LiveUnitWithOrder, StartTripleCastLiveResult
    from progression import context

    payload = await read_request(request, StartTripleCastLivePayload)
    window = window_for('triple_cast', getattr(payload, 'm_triple_cast_id', None)) if payload else None
    chart = master('live_master', getattr(payload, 'm_live_id', 0)) if payload else None
    if not window or not chart or window.music != chart.music_master_id:
        return respond(StartTripleCastLiveResult(), faults=[fault('InvalidLiveStage')])
    try:
        async with transaction(request) as s:
            await begin(s, 'triple_cast', window)
            mine = await basic(s, 'triple_cast')
            parties = await s.rows('TripleCastParty')
            slots = await s.rows('TripleCastPartySlot')
            chosen = {}
            units = []
            for order, party_order in enumerate(
                (mine['partyOrder1'], mine['partyOrder2'], mine['partyOrder3']), start=1
            ):
                party = next((p for p in parties if int(p['order']) == int(party_order)), None)
                if party is None:
                    raise Rejected('Missing triple cast party')
                chosen[order] = party['id']
                view = _TripleCastView(s.conn, [x for x in slots if x['partyId'] == party['id']],
                                       [party])
                unit, live_id = await build_live_unit(view, s.uid, party['id'], payload.m_live_id)
                notation = await build_live_time_event(
                    view, s.uid, party['id'], window.music, window.sense_ids[order - 1])
                # LiveUnitWithOrder.actors / .time_events are MessagePack maps, held as
                # plain dicts so to_wire writes them as maps of encoded models. Rebuilding
                # the unit through the model constructor would validate those dicts against
                # the empty Dictionary model and throw them away, and model_dump() would
                # turn the nested live models into string-keyed dicts, so the values are
                # carried over as they are.
                ordered = LiveUnitWithOrder()
                for name in ('actors', 'time_events', 'possible_senses', 'start_effects',
                             'star_act', 'total_status', 'star_act_sense_light_count',
                             'max_principal', 'is_first_play_olivier', 'base_score_percentage',
                             'base_score_difficulty_auto_coefficient'):
                    setattr(ordered, name, getattr(unit, name))
                ordered.time_events = {event.timing_seconds: event for event in notation.timings}
                ordered.u_active_live_id = live_id
                ordered.order = order
                units.append(ordered)
            # A ladder run is free to replay: nothing is charged, and the active live
            # records that no stamina was spent (ladder_stamina_cost).
            await s.conn.execute(delete_active_lives(s.uid))
            await s.conn.execute(create_active_live(
                s.uid, units[0].u_active_live_id, payload.m_live_id, 0, 0, False))
            await context(s, 'triple_cast', window.id, {'orders': chosen, 'auto': False})
            present = s.present()
        return respond(StartTripleCastLiveResult(live_units=units), present=present)
    except Rejected as exc:
        return respond(StartTripleCastLiveResult(), faults=[fault('InvalidLiveStage', str(exc))])


async def score_ladder(s, context, active, payload, result):
    """Post a finished ladder run: one party for the league, three for a triple cast."""
    kind = 'league' if context['mode'] == 'league' else 'triple_cast'
    window = window_for(kind, context['masterId'])
    if window is None:
        raise Rejected('Missing league window')
    chart = master('live_master', active['liveMasterId'])
    difficulty = int(chart.difficulty) if chart else 0
    total_score = play_totals(payload)[0]
    if kind == 'league':
        cards = await party_cards(s, active['partyId'])
        party = await s.one('Party', id=active['partyId'])
        return await score_league(s, kind, window, cards, total_score, difficulty,
                                  int(party['leaderPosition'] if party else 1), result)
    orders = {int(k): int(v) for k, v in (context['extra'].get('orders') or {}).items()}
    posted = {int(block.order): int(block.score) for block in payload.triple_cast_party_scores or []}
    total = sum(posted.values()) if posted else int(total_score)
    for order, party_row in orders.items():
        cards = await party_cards(s, party_row, triple=True)
        party = await s.one('TripleCastParty', id=party_row)
        await post(s, kind, window, posted.get(order, 0), cards, difficulty,
                   int(party['leaderPosition'] if party else 1), order=order, total=total)
    if not orders:
        entry = await member(s, kind, window)
        await write(s, SPEC[kind]['member'], {SPEC[kind]['window']: window.id},
                    {'bestScore': max(int((entry or {}).get('bestScore') or 0), int(total))})
        row = await history(s, kind, window)
        if row and not row['isPlayed']:
            await write(s, SPEC[kind]['history'], {SPEC[kind]['window']: window.id},
                        {'isPlayed': True})
    board = await standings(s, kind, window)
    result.league_result = await result_fields(s, kind, window, board)
    things = play_reward_things(window)
    if things:
        result.league_rewards = await s.grant(things)
    return total


async def ensure_field_table(conn):
    """The table the generated field lives in - never an official entity table."""
    await conn.conn.execute(
        f'CREATE TABLE IF NOT EXISTS {RIVAL_TABLE} ('
        '"userId" bigint NOT NULL REFERENCES accounts("userId") ON DELETE CASCADE, '
        'kind text NOT NULL, "masterId" integer NOT NULL, "classType" integer NOT NULL, '
        'ordinal integer NOT NULL, "userHash" text NOT NULL, name text NOT NULL, '
        'score bigint NOT NULL, party jsonb NOT NULL, '
        'PRIMARY KEY ("userId", kind, "masterId", "classType", ordinal))'
    )


def install(app):
    """Extend the master cache, add the table the generated field lives in, wire the routes."""
    from contextlib import asynccontextmanager

    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with previous(app):
            schedule.extend_cache()
            async with app.acquire_db() as conn:
                await ensure_field_table(conn)
            yield
    app.router.lifespan_context = lifespan

    count = len(app.router.routes)
    app.include_router(router)
    app.router.routes[:] = app.router.routes[count:] + app.router.routes[:count]

    # The one live rule the ladder adds: a ladder run is priced at no stamina. It is
    # installed last so the price captured here is whatever the other overlays left.
    global _upstream_stamina_cost
    from routes import lives as live_route
    _upstream_stamina_cost = live_route._stamina_cost
    live_route._stamina_cost = _stamina_cost
