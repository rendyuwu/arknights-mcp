"""Internal enemy intel service (§T35): the single domain entry point both
transports call to fetch one enemy's facts (§V14).

Given a read-only SQLite connection and a ``(server, game_id)`` selector, it loads
the enemy's typed facts + region + provenance (§V5) and its level variants (the
stat block plus the allowlisted structural JSON fragments, decoded here). The
service adds no natural-language interpretation of its own -- it emits only typed,
vetted fields.

Read-only + parameterized SQL only (§V2): the parameterized ``SELECT``s live in
:class:`~arknights_mcp.db.repositories.enemies.EnemyRepository` (§T20), the sole
sanctioned SQL surface; this service only reads through it and never mutates the
database. It does not open the connection (callers pass one in), so both
transports share this exact function (§V14). No transport-specific logic lives
here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from arknights_mcp.db.repositories.enemies import EnemyLevelRow, EnemyRepository, EnemyRow
from arknights_mcp.util.coerce import json_load

#: Typed outcome of an enemy lookup. The full §V23 status vocabulary is wired into
#: the tool envelope (§T29); this service reports only these two.
EnemyLookupStatus = Literal["ok", "not_found"]


@dataclass(frozen=True)
class EnemyProvenance:
    """Region-scoped provenance for a factual enemy response (§V5)."""

    snapshot_id: str
    imported_at: str


@dataclass(frozen=True)
class EnemyLevelFacts:
    """One level variant of an enemy: the typed stat block + decoded structural
    JSON (targeting/immunities/abilities were allowlisted + sanitized at import,
    §V18/§V31, so decoding re-exposes only vetted data)."""

    level_variant: int
    hp: int | None
    atk: int | None
    def_: int | None
    res: int | None
    attack_interval: float | None
    attack_range: float | None
    move_speed: float | None
    weight: int | None
    life_point_reduction: int | None
    block_behavior: str | None
    targeting: object | None
    immunities: object | None
    abilities: object | None


@dataclass(frozen=True)
class EnemyFacts:
    """Typed, allowlisted facts about one enemy (no prose; §V16, §V18).

    Carries region (``server``) + provenance (§V5). ``levels`` is the enemy's
    ordered stat block (bounded -- an enemy has a small, fixed set of variants).
    """

    server: str
    game_id: str
    display_name: str | None
    enemy_class: str | None
    is_boss: bool
    is_elite: bool
    attack_type: str | None
    motion_type: str | None
    levels: tuple[EnemyLevelFacts, ...]
    provenance: EnemyProvenance


@dataclass(frozen=True)
class EnemyDetailResult:
    """Domain result of :func:`get_enemy`.

    ``status == "not_found"`` implies ``enemy is None``. An ``ok`` result carries
    region + provenance on ``enemy`` (§V5).
    """

    status: EnemyLookupStatus
    server: str
    enemy: EnemyFacts | None


def _level_facts(level: EnemyLevelRow) -> EnemyLevelFacts:
    """Shape a level row into typed facts, decoding the stored JSON fragments."""
    return EnemyLevelFacts(
        level_variant=level.level_variant,
        hp=level.hp,
        atk=level.atk,
        def_=level.def_,
        res=level.res,
        attack_interval=level.attack_interval,
        attack_range=level.attack_range,
        move_speed=level.move_speed,
        weight=level.weight,
        life_point_reduction=level.life_point_reduction,
        block_behavior=level.block_behavior,
        targeting=json_load(level.targeting_json),
        immunities=json_load(level.immunities_json),
        abilities=json_load(level.abilities_json),
    )


def _enemy_facts(enemy: EnemyRow, levels: tuple[EnemyLevelFacts, ...]) -> EnemyFacts:
    """Shape a repository row into the typed, region-attributed facts (§V5).

    The enemy join is on NOT NULL foreign keys, so ``provenance`` (snapshot_id +
    imported_at) is always present on an ``ok`` result.
    """
    return EnemyFacts(
        server=enemy.server,
        game_id=enemy.game_id,
        display_name=enemy.display_name,
        enemy_class=enemy.enemy_class,
        is_boss=enemy.is_boss,
        is_elite=enemy.is_elite,
        attack_type=enemy.attack_type,
        motion_type=enemy.motion_type,
        levels=levels,
        provenance=EnemyProvenance(snapshot_id=enemy.snapshot_id, imported_at=enemy.imported_at),
    )


def get_enemy(
    conn: sqlite3.Connection,
    *,
    server: str,
    game_id: str,
) -> EnemyDetailResult:
    """Fetch one enemy's facts + level variants for ``server`` (§T35; §V5/§V23).

    Read-only; parameterized SQL only (§V2). The enemy is resolved by its unique
    ``(server, game_id)`` key, so an ``en`` enemy is never surfaced under a ``cn``
    query (§V5). A missing enemy returns ``status == "not_found"`` (the tool maps
    it to the typed §V23 envelope). Both transports call this function (§V14).
    """
    repo = EnemyRepository(conn)
    enemy = repo.enemy_by_game_id(server, game_id)
    if enemy is None:
        return EnemyDetailResult(status="not_found", server=server, enemy=None)

    levels = tuple(_level_facts(level) for level in repo.enemy_levels(enemy.enemy_pk))
    return EnemyDetailResult(
        status="ok",
        server=enemy.server,
        enemy=_enemy_facts(enemy, levels),
    )
