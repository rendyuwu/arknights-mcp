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
    ACTIVITY_ALLOWLIST,
    STAGE_ALLOWLIST,
    ZONE_ALLOWLIST,
    apply_allowlist,
)
from arknights_mcp.importers.levels import LevelImportResult, insert_level, parse_level
from arknights_mcp.importers.manifest import insert_record_provenance
from arknights_mcp.importers.normalization import (
    is_clean_level_path,
    normalize_level,
    normalize_level_id,
)
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
class ParsedActivity:
    """One event's TITLE, as the client would name it (§V110/B155).

    Distinct from :class:`ParsedZone`'s ``display_name``, which is the sub-zone
    SUBTITLE ``zone_table`` carries ("The Coming of The Future"). The title
    ("Lone Trail") exists only in ``activity_table``, so a zone holds both.
    """

    game_id: str
    display_name: str
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
    #: Zones that got an event TITLE from ``activity_table`` (§V110). 0 is legitimate
    #: only when the snapshot carries no activity table at all; with the table present
    #: the importer fails closed rather than shipping a title-less index (B155).
    zone_event_names: int = 0


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
        # T179 review-fix: the REAL zone_table names a zone via ``zoneNameSecond``
        # (no ``zoneName`` key -- see tests/fixtures/stage_4_4_real); reading only
        # ``zoneName`` left display_name NULL on real builds, which nulled the §T179
        # stage search alias ("Lone Trail" -> its stages) the tool descriptions
        # promise. Prefer ``zoneName`` (synthetic/back-compat), fall back to the
        # real-shape key.
        out.append(
            ParsedZone(
                game_id=game_id,
                display_name=as_str(kept.get("zoneName")) or as_str(kept.get("zoneNameSecond")),
                zone_type=as_str(kept.get("type")),
                provenance_record=kept,
            )
        )
    return out


def parse_activity_titles(activity_raw: Any) -> dict[str, ParsedActivity]:
    """Map each zone game_id to its event title, from ``activity_table`` (§V110).

    Two source keys, both required: ``basicInfo[<actId>].name`` holds the title, and
    ``zoneToActivity`` maps a zone id onto its ``actId``. The join is the whole point
    -- a zone knows nothing about its event, and the event record knows nothing about
    its zones (B155: at the pinned commit this resolves 266 of 429 EN zones onto 120
    distinct titles; a zone with no activity row, e.g. ``camp_zone_*`` annihilation or
    ``tower_*``, is simply absent from the returned map and keeps a NULL title, §V26).

    Raises :class:`ImporterError` when the table is present but shaped otherwise, or
    when it yields no titles at all despite a non-empty ``basicInfo`` -- a silent-empty
    title map is exactly the state B155 shipped in, and it is invisible downstream
    (every zone still has its subtitle, so nothing else looks wrong, §V30).
    """
    if not isinstance(activity_raw, dict):
        raise ImporterError("activity table is not a JSON object")
    basic_info = activity_raw.get("basicInfo")
    zone_to_activity = activity_raw.get("zoneToActivity")
    if not isinstance(basic_info, dict) or not isinstance(zone_to_activity, dict):
        raise ImporterError("activity table missing top-level 'basicInfo' / 'zoneToActivity'")

    titles: dict[str, ParsedActivity] = {}
    for act_id in sorted(basic_info):
        entry = basic_info[act_id]
        if not isinstance(entry, dict):
            continue
        kept = apply_allowlist(entry, ACTIVITY_ALLOWLIST).kept
        display_name = as_str(kept.get("name"))
        if display_name is None:
            continue
        titles[act_id] = ParsedActivity(
            game_id=act_id, display_name=display_name, provenance_record=kept
        )

    by_zone: dict[str, ParsedActivity] = {}
    for zone_id in sorted(zone_to_activity):
        act_id = zone_to_activity[zone_id]
        if not isinstance(act_id, str):
            continue
        activity = titles.get(act_id)
        if activity is not None:
            by_zone[zone_id] = activity
    if basic_info and not by_zone:
        raise ImporterError(
            "activity table yielded no event titles: basicInfo has "
            f"{len(basic_info)} entries but zoneToActivity resolved none (§V110/B155)"
        )
    return by_zone


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
    activity_table_path: str = "gamedata/excel/activity_table.json",
) -> StageImportResult:
    """Import zones + stages for ``adapter.server`` and each stage's level file."""
    server = adapter.server
    zones = parse_zones(adapter.read_json(zone_table_path))
    stages = parse_stages(adapter.read_json(stage_table_path))
    # §V110/B155: the event TITLE lives in activity_table, never in zone_table. The
    # table is fetched tolerant-absent (§V41/B36) -- a combat-only snapshot lacking it
    # imports every zone with a NULL event_name -- but when it IS there its shape is
    # contract, so parse_activity_titles fails closed on a silent-empty map.
    activity_by_zone: dict[str, ParsedActivity] = {}
    if adapter.exists(activity_table_path):
        activity_by_zone = parse_activity_titles(adapter.read_json(activity_table_path))
    else:
        _LOG.warning(
            "snapshot has no %s: zones import with no event title, so an event is not "
            "searchable by name (§V110)",
            activity_table_path,
        )
    enemy_pk_by_game_id = _enemy_pk_by_game_id(conn, server)

    # One provenance row per ACTIVITY record, shared by that event's zones (the title
    # is one source fact, not one per zone). Its source_path is the activity table, so
    # the row never claims the zone table said something it did not (§V17).
    activity_provenance: dict[str, int] = {}
    zone_pk_by_game_id: dict[str, int] = {}
    zone_event_names = 0
    for zone in zones:
        provenance_id = insert_record_provenance(
            conn,
            snapshot_id=snapshot_id,
            source_path=zone_table_path,
            source_record_key=zone.game_id,
            record=zone.provenance_record,
        )
        activity = activity_by_zone.get(zone.game_id)
        event_provenance_id: int | None = None
        if activity is not None:
            event_provenance_id = activity_provenance.get(activity.game_id)
            if event_provenance_id is None:
                event_provenance_id = insert_record_provenance(
                    conn,
                    snapshot_id=snapshot_id,
                    source_path=activity_table_path,
                    source_record_key=activity.game_id,
                    record=activity.provenance_record,
                )
                activity_provenance[activity.game_id] = event_provenance_id
            zone_event_names += 1
        cur = conn.execute(
            "INSERT INTO zones (server, game_id, display_name, zone_type, provenance_id, "
            "event_name, activity_game_id, event_provenance_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                server,
                zone.game_id,
                zone.display_name,
                zone.zone_type,
                provenance_id,
                activity.display_name if activity is not None else None,
                activity.game_id if activity is not None else None,
                event_provenance_id,
            ),
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
        # Confine the normalized path to the levels tree before it is stored or read
        # (§V36; B17). A crafted levelId can fold back into gamedata/excel or escape
        # the tree via "..", staying inside the snapshot root so the adapter's
        # _safe_path passes; the same clean-path gate the network discovery uses must
        # also guard the local import path, else a stage reads an excel table as a
        # level file. A non-clean reference is dropped (stage imported with no level).
        if level_path is not None and not is_clean_level_path(level_path):
            _LOG.warning(
                "stage %s: levelId %r normalized to %r which is outside the levels "
                "tree; refusing to read it as a level file (§V36)",
                stage.game_id,
                stage.level_id,
                level_path,
            )
            level_path = None
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
                variants=totals.variants + result.variants,
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
        zone_event_names=zone_event_names,
    )
