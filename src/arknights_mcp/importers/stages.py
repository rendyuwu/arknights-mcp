"""Stage importer: stage_table + zone_table -> zones + stages, and each stage's
level file -> map/tiles/routes/waves/spawns + stage_enemies (via ``levels``).

Applies the field allowlist + sanitization (§V18) and attaches per-record
provenance (§V17). Spawns resolve to enemies already imported for the region.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.field_policy import (
    STAGE_ALLOWLIST,
    ZONE_ALLOWLIST,
    apply_allowlist,
)
from arknights_mcp.importers.levels import LevelImportResult, insert_level, parse_level
from arknights_mcp.importers.manifest import insert_record_provenance
from arknights_mcp.importers.normalization import normalize_level, normalize_level_id
from arknights_mcp.sources.base import SourceAdapter
from arknights_mcp.util.coerce import as_int, as_str

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedZone:
    game_id: str
    display_name: str | None
    zone_type: str | None
    provenance_record: dict[str, Any]


@dataclass(frozen=True)
class ParsedStage:
    game_id: str
    stage_code: str | None
    display_name: str | None
    zone_game_id: str | None
    stage_type: str | None
    difficulty: str | None
    sanity_cost: int | None
    recommended_level: int | None
    max_life_points: int | None
    level_id: str | None
    provenance_record: dict[str, Any]


@dataclass(frozen=True)
class StageImportResult:
    zones_inserted: int
    stages_inserted: int
    levels: LevelImportResult
    #: Stages that named a level file, and of those how many were resolved+imported.
    #: A non-empty combat source with references but 0 imports (or 0 downstream
    #: rows) is a silent-empty regression the pipeline fails closed on (§V30).
    levels_referenced: int = 0
    levels_imported: int = 0


def parse_zones(zone_raw: Any) -> list[ParsedZone]:
    if not isinstance(zone_raw, dict) or "zones" not in zone_raw:
        raise ImporterError("zone table missing top-level 'zones'")
    zones: dict[str, Any] = zone_raw["zones"]
    out: list[ParsedZone] = []
    for game_id in sorted(zones):
        entry = zones[game_id]
        if not isinstance(entry, dict):
            continue
        kept = apply_allowlist(entry, ZONE_ALLOWLIST).kept
        out.append(
            ParsedZone(
                game_id=game_id,
                display_name=as_str(kept.get("zoneName")),
                zone_type=as_str(kept.get("type")),
                provenance_record=kept,
            )
        )
    return out


def parse_stages(stage_raw: Any) -> list[ParsedStage]:
    if not isinstance(stage_raw, dict) or "stages" not in stage_raw:
        raise ImporterError("stage table missing top-level 'stages'")
    stages: dict[str, Any] = stage_raw["stages"]
    out: list[ParsedStage] = []
    for game_id in sorted(stages):
        entry = stages[game_id]
        if not isinstance(entry, dict):
            continue
        kept = apply_allowlist(entry, STAGE_ALLOWLIST).kept
        out.append(
            ParsedStage(
                game_id=game_id,
                stage_code=as_str(kept.get("code")),
                display_name=as_str(kept.get("name")),
                zone_game_id=as_str(kept.get("zoneId")),
                stage_type=as_str(kept.get("stageType")),
                difficulty=as_str(kept.get("difficulty")),
                sanity_cost=as_int(kept.get("apCost")),
                recommended_level=as_int(kept.get("recommendedLevel")),
                max_life_points=as_int(kept.get("maxLifePoints")),
                level_id=as_str(kept.get("levelId")),
                provenance_record=kept,
            )
        )
    return out


def _enemy_pk_by_game_id(conn: sqlite3.Connection, server: str) -> dict[str, int]:
    return {
        game_id: enemy_pk
        for game_id, enemy_pk in conn.execute(
            "SELECT game_id, enemy_pk FROM enemies WHERE server = ?", (server,)
        )
    }


def import_stages(
    conn: sqlite3.Connection,
    adapter: SourceAdapter,
    snapshot_id: str,
    *,
    stage_table_path: str = "gamedata/excel/stage_table.json",
    zone_table_path: str = "gamedata/excel/zone_table.json",
) -> StageImportResult:
    """Import zones + stages for ``adapter.server`` and each stage's level file."""
    server = adapter.server
    zones = parse_zones(adapter.read_json(zone_table_path))
    stages = parse_stages(adapter.read_json(stage_table_path))
    enemy_pk_by_game_id = _enemy_pk_by_game_id(conn, server)

    zone_pk_by_game_id: dict[str, int] = {}
    for zone in zones:
        provenance_id = insert_record_provenance(
            conn,
            snapshot_id=snapshot_id,
            source_path=zone_table_path,
            source_record_key=zone.game_id,
            record=zone.provenance_record,
        )
        cur = conn.execute(
            "INSERT INTO zones (server, game_id, display_name, zone_type, provenance_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (server, zone.game_id, zone.display_name, zone.zone_type, provenance_id),
        )
        zone_pk_by_game_id[zone.game_id] = int(cur.lastrowid or 0)

    totals = LevelImportResult()
    stages_inserted = 0
    levels_referenced = 0
    levels_imported = 0
    for stage in stages:
        provenance_id = insert_record_provenance(
            conn,
            snapshot_id=snapshot_id,
            source_path=stage_table_path,
            source_record_key=stage.game_id,
            record=stage.provenance_record,
        )
        # Real levelId is a Title-case, extension-less reference; rewrite it to the
        # actual snapshot path (§V29/§V30). A no-op for an already-resolvable path.
        level_path = normalize_level_id(stage.level_id)
        zone_pk = (
            zone_pk_by_game_id.get(stage.zone_game_id) if stage.zone_game_id is not None else None
        )
        cur = conn.execute(
            "INSERT INTO stages "
            "(server, game_id, stage_code, display_name, zone_pk, stage_type, difficulty, "
            "sanity_cost, recommended_level, max_life_points, level_source_path, provenance_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                server,
                stage.game_id,
                stage.stage_code,
                stage.display_name,
                zone_pk,
                stage.stage_type,
                stage.difficulty,
                stage.sanity_cost,
                stage.recommended_level,
                stage.max_life_points,
                level_path,
                provenance_id,
            ),
        )
        stage_pk = int(cur.lastrowid or 0)
        stages_inserted += 1

        if level_path:
            levels_referenced += 1
        if level_path and adapter.exists(level_path):
            raw_level = normalize_level(adapter.read_json(level_path))
            # The level file is a distinct source_path from the stage table, so it
            # gets its own provenance row; every level-derived row links to it (§V17).
            level_provenance_id = insert_record_provenance(
                conn,
                snapshot_id=snapshot_id,
                source_path=level_path,
                source_record_key=stage.game_id,
                record=raw_level,
            )
            level = parse_level(raw_level)
            result = insert_level(
                conn, stage_pk, level, enemy_pk_by_game_id, provenance_id=level_provenance_id
            )
            levels_imported += 1
            totals = LevelImportResult(
                tiles=totals.tiles + result.tiles,
                routes=totals.routes + result.routes,
                waves=totals.waves + result.waves,
                spawns=totals.spawns + result.spawns,
                stage_enemies=totals.stage_enemies + result.stage_enemies,
            )
        elif level_path:
            # A stage that names a level file we cannot resolve is imported with no
            # map/waves/spawns; record it rather than silently returning an empty,
            # wrong picture to analysis (§21.2 unresolved cross-reference).
            _LOG.warning(
                "stage %s references level file %r (from levelId %r) which is absent; "
                "imported with no map/tiles/routes/waves/spawns",
                stage.game_id,
                level_path,
                stage.level_id,
            )

    return StageImportResult(
        zones_inserted=len(zones),
        stages_inserted=stages_inserted,
        levels=totals,
        levels_referenced=levels_referenced,
        levels_imported=levels_imported,
    )
