"""Enemy importer: enemy_handbook + enemy_database -> enemies + enemy_levels.

Applies the field allowlist and string sanitization (§V18) and attaches record
provenance (§V17). Pure parsing (:func:`parse_enemies`) is separated from
insertion so it is unit-testable without a database.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from arknights_mcp.importers.field_policy import (
    ENEMY_HANDBOOK_ALLOWLIST,
    ENEMY_LEVEL_ALLOWLIST,
    apply_allowlist,
)
from arknights_mcp.importers.manifest import make_record_provenance
from arknights_mcp.sources.base import SourceAdapter


class ImporterError(ValueError):
    """Raised when source data is missing required top-level structure."""


@dataclass(frozen=True)
class ParsedEnemyLevel:
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
    targeting: Any
    immunities: Any
    abilities: Any


@dataclass(frozen=True)
class ParsedEnemy:
    game_id: str
    display_name: str | None
    enemy_class: str | None
    is_boss: bool
    is_elite: bool
    attack_type: str | None
    motion_type: str | None
    levels: list[ParsedEnemyLevel]
    provenance_record: dict[str, Any]


@dataclass(frozen=True)
class EnemyImportResult:
    enemies_inserted: int
    levels_inserted: int


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, bool | int | float) else None


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, bool | int | float) else None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def parse_enemies(handbook_raw: Any, database_raw: Any) -> list[ParsedEnemy]:
    """Transform raw handbook + database JSON into typed, allowlisted enemies."""
    if not isinstance(handbook_raw, dict) or "enemyData" not in handbook_raw:
        raise ImporterError("enemy handbook missing top-level 'enemyData'")
    if not isinstance(database_raw, dict) or "enemies" not in database_raw:
        raise ImporterError("enemy database missing top-level 'enemies'")

    handbook: dict[str, Any] = handbook_raw["enemyData"]
    database: dict[str, Any] = database_raw["enemies"]
    parsed: list[ParsedEnemy] = []

    # Drive from the union of both key sets so an enemy present only in the stats
    # database (no handbook entry) is still imported with its levels; otherwise a
    # stage spawn referencing it fails closed with "unknown enemy" (§21.2).
    for game_id in sorted(set(handbook) | set(database)):
        entry = handbook.get(game_id, {})
        if not isinstance(entry, dict):
            continue
        kept_hb = apply_allowlist(entry, ENEMY_HANDBOOK_ALLOWLIST).kept
        enemy_class = _as_str(kept_hb.get("enemyLevel"))

        levels: list[ParsedEnemyLevel] = []
        kept_levels: list[dict[str, Any]] = []
        db_entry = database.get(game_id, {})
        raw_levels = db_entry.get("levels", []) if isinstance(db_entry, dict) else []
        for raw_level in raw_levels:
            if not isinstance(raw_level, dict):
                continue
            kept = apply_allowlist(raw_level, ENEMY_LEVEL_ALLOWLIST).kept
            kept_levels.append(kept)
            levels.append(
                ParsedEnemyLevel(
                    level_variant=_as_int(kept.get("level")) or 0,
                    hp=_as_int(kept.get("hp")),
                    atk=_as_int(kept.get("atk")),
                    def_=_as_int(kept.get("def")),
                    res=_as_int(kept.get("res")),
                    attack_interval=_as_float(kept.get("attackInterval")),
                    attack_range=_as_float(kept.get("attackRange")),
                    move_speed=_as_float(kept.get("moveSpeed")),
                    weight=_as_int(kept.get("weight")),
                    life_point_reduction=_as_int(kept.get("lifePointReduction")),
                    block_behavior=_as_str(kept.get("blockBehavior")),
                    targeting=kept.get("targeting"),
                    immunities=kept.get("immunities"),
                    abilities=kept.get("abilities"),
                )
            )

        parsed.append(
            ParsedEnemy(
                game_id=game_id,
                display_name=_as_str(kept_hb.get("name")),
                enemy_class=enemy_class,
                is_boss=enemy_class == "BOSS",
                is_elite=enemy_class == "ELITE",
                attack_type=_as_str(kept_hb.get("attackType")),
                motion_type=_as_str(kept_hb.get("motionType")),
                levels=levels,
                provenance_record={"handbook": kept_hb, "levels": kept_levels},
            )
        )
    return parsed


def insert_enemies(
    conn: sqlite3.Connection,
    parsed: list[ParsedEnemy],
    *,
    server: str,
    snapshot_id: str,
    handbook_source_path: str,
) -> EnemyImportResult:
    """Insert parsed enemies + levels, attaching per-record provenance (§V17)."""
    enemies_inserted = 0
    levels_inserted = 0
    for enemy in parsed:
        prov = make_record_provenance(
            snapshot_id=snapshot_id,
            source_path=handbook_source_path,
            source_record_key=enemy.game_id,
            record=enemy.provenance_record,
        )
        cur = conn.execute(
            "INSERT INTO record_provenance "
            "(snapshot_id, source_path, source_record_key, record_hash, "
            "transform_version, field_policy_version) VALUES (?, ?, ?, ?, ?, ?)",
            (
                prov.snapshot_id,
                prov.source_path,
                prov.source_record_key,
                prov.record_hash,
                prov.transform_version,
                prov.field_policy_version,
            ),
        )
        provenance_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO enemies "
            "(server, game_id, display_name, enemy_class, is_boss, is_elite, "
            "attack_type, motion_type, provenance_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                server,
                enemy.game_id,
                enemy.display_name,
                enemy.enemy_class,
                int(enemy.is_boss),
                int(enemy.is_elite),
                enemy.attack_type,
                enemy.motion_type,
                provenance_id,
            ),
        )
        enemy_pk = cur.lastrowid
        enemies_inserted += 1
        for level in enemy.levels:
            try:
                conn.execute(
                    "INSERT INTO enemy_levels "
                    "(enemy_pk, level_variant, hp, atk, def, res, attack_interval, "
                    "attack_range, move_speed, weight, life_point_reduction, block_behavior, "
                    "targeting_json, immunities_json, abilities_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        enemy_pk,
                        level.level_variant,
                        level.hp,
                        level.atk,
                        level.def_,
                        level.res,
                        level.attack_interval,
                        level.attack_range,
                        level.move_speed,
                        level.weight,
                        level.life_point_reduction,
                        level.block_behavior,
                        _json_or_none(level.targeting),
                        _json_or_none(level.immunities),
                        _json_or_none(level.abilities),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # A repeated or absent level index collides on UNIQUE(enemy_pk,
                # level_variant); fail closed with a clear message instead of an
                # uncaught traceback that tears down the whole build (§V3).
                raise ImporterError(
                    f"enemy {enemy.game_id!r} has a duplicate level_variant "
                    f"{level.level_variant}: {exc}"
                ) from exc
            levels_inserted += 1
    return EnemyImportResult(enemies_inserted=enemies_inserted, levels_inserted=levels_inserted)


def import_enemies(
    conn: sqlite3.Connection,
    adapter: SourceAdapter,
    snapshot_id: str,
    *,
    handbook_path: str = "gamedata/excel/enemy_handbook_table.json",
    database_path: str = "gamedata/levels/enemydata/enemy_database.json",
) -> EnemyImportResult:
    """Read enemy files via the adapter and import them for ``adapter.server``."""
    handbook_raw = adapter.read_json(handbook_path)
    database_raw = adapter.read_json(database_path)
    parsed = parse_enemies(handbook_raw, database_raw)
    return insert_enemies(
        conn,
        parsed,
        server=adapter.server,
        snapshot_id=snapshot_id,
        handbook_source_path=handbook_path,
    )
