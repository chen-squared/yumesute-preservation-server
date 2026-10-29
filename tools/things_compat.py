"""Dual-idol character grants for the pinned vendor (upstream 830b442).

A dual-idol CharacterMaster carries a second CharacterBase. The pinned grant
path only creates the first one, so the client can hang on party/edit screens
when such a card is granted. Patch the grant helper and add a safe repair for
rows granted before this fix.
"""

import random
from typing import Optional

from db.user import create_character_base, get_character_base
from helpers import things
from helpers.cache import cache


def _by_id(table: str) -> dict:
    return things._by_id(table)


def _random_id() -> int:
    return random.randint(1_000_000, 9_999_999_999)


async def _owned_base(
    conn, user_id: int, base_master_id: int, character_master_id: int
) -> int:
    existing = await conn.fetchrow(get_character_base(user_id, base_master_id))
    if existing is not None:
        return existing.id
    base_id = _random_id()
    bm = _by_id("character_base_master").get(base_master_id)
    costume = bm.default_costume_master_id if bm is not None else None
    await conn.execute(
        create_character_base(
            user_id, base_id, base_master_id, costume, character_master_id
        )
    )
    return base_id


async def _grant_character(conn, user_id: int, character_master_id: int) -> None:
    master = _by_id("character_master").get(character_master_id)
    base_master_id = master.character_base_master_id if master is not None else None
    secondary_master_id = (
        master.secondary_character_base_master_id if master is not None else None
    )
    base_id = 0
    if base_master_id is not None:
        base_id = await _owned_base(
            conn, user_id, base_master_id, character_master_id
        )
    secondary_id: Optional[int] = None
    if secondary_master_id is not None:
        secondary_id = await _owned_base(
            conn, user_id, secondary_master_id, character_master_id
        )
    await conn.conn.execute(
        'INSERT INTO "character" '
        '("userId", "id", "characterMasterId", "characterBaseId", "level", '
        '"senseLevel", "secondaryCharacterBaseId", "secondarySenseLevel", '
        '"selectionType") VALUES ($1,$2,$3,$4,1,1,$5,$6,1)',
        user_id,
        _random_id(),
        character_master_id,
        base_id,
        secondary_id,
        1 if secondary_id is not None else 0,
    )


def _dual_masters() -> dict:
    return {
        m.id_: m.secondary_character_base_master_id
        for m in cache.character_master
        if m.secondary_character_base_master_id is not None
    }


async def repair_dual_cards(conn) -> int:
    """Link the second idol on saved dual cards that are missing it."""
    duals = _dual_masters()
    if not duals:
        return 0
    rows = await conn.conn.fetch(
        'SELECT "userId", "id", "characterMasterId" FROM "character" '
        'WHERE "secondaryCharacterBaseId" IS NULL '
        'AND "characterMasterId" = ANY($1::bigint[])',
        list(duals),
    )
    for row in rows:
        master_id = row["characterMasterId"]
        base_id = await _owned_base(
            conn, row["userId"], duals[master_id], master_id
        )
        await conn.conn.execute(
            'UPDATE "character" SET "secondaryCharacterBaseId"=$3, '
            '"secondarySenseLevel"=GREATEST("secondarySenseLevel",1) '
            'WHERE "userId"=$1 AND "id"=$2 AND "secondaryCharacterBaseId" IS NULL',
            row["userId"],
            row["id"],
            base_id,
        )
    return len(rows)


_INSTALLED = False


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    things._grant_character = _grant_character
    things.repair_dual_cards = repair_dual_cards
