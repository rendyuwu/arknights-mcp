"""Level parser: map / tiles / routes / waves / spawns + derived stage_enemies.

Reads the allowlisted structural fields of a stage's level file (§V18 — known
keys read explicitly, string fields sanitized, no prose) and writes the map,
tiles, routes, waves, and spawns for one stage, then derives the
``stage_enemies`` summary. Cross-file references (spawn -> enemy) must resolve
(§21.2); an unresolved enemy fails closed.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.util.text import sanitize_text


@dataclass(frozen=True)
class ParsedTile:
    x: int
    y: int
    tile_key: str | None
    height_type: str | None
    buildable_type: str | None
    passable: bool | None
    special_properties: Any


@dataclass(frozen=True)
class ParsedRoute:
    route_index: int
    start_position: Any
    end_position: Any
    checkpoints: Any


@dataclass(frozen=True)
class ParsedSpawn:
    enemy_game_id: str
    level_variant: int
    route_index: int | None
    spawn_time: float | None
    count: int | None
    interval: float | None
    spawn_group: str | None
    hidden: bool
    source_fragment: dict[str, Any]


@dataclass(frozen=True)
class ParsedWave:
    wave_index: int
    pre_delay: float | None
    max_time_waiting: float | None
    spawns: list[ParsedSpawn]


@dataclass(frozen=True)
class ParsedLevel:
    width: int | None
    height: int | None
    map_version: str | None
    environment: Any
    tiles: list[ParsedTile]
    routes: list[ParsedRoute]
    waves: list[ParsedWave]


@dataclass(frozen=True)
class LevelImportResult:
    tiles: int = 0
    routes: int = 0
    waves: int = 0
    spawns: int = 0
    stage_enemies: int = 0


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, bool | int | float) else None


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, bool | int | float) else None


def _as_str(value: Any) -> str | None:
    return sanitize_text(value) if isinstance(value, str) else None


def _json_or_none(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)


def parse_level(level_raw: Any) -> ParsedLevel:
    """Transform a raw level JSON document into typed structural data."""
    if not isinstance(level_raw, dict):
        raise ImporterError("level file is not a JSON object")
    map_data = level_raw.get("mapData", {})
    if not isinstance(map_data, dict):
        map_data = {}

    tiles: list[ParsedTile] = []
    for raw in map_data.get("tiles", []):
        if not isinstance(raw, dict):
            continue
        x, y = _as_int(raw.get("x")), _as_int(raw.get("y"))
        if x is None or y is None:
            continue
        passable = raw.get("passable")
        tiles.append(
            ParsedTile(
                x=x,
                y=y,
                tile_key=_as_str(raw.get("tileKey")),
                height_type=_as_str(raw.get("heightType")),
                buildable_type=_as_str(raw.get("buildableType")),
                passable=bool(passable) if isinstance(passable, bool) else None,
                special_properties=raw.get("specialProperties"),
            )
        )

    routes: list[ParsedRoute] = []
    for raw in level_raw.get("routes", []):
        if not isinstance(raw, dict):
            continue
        idx = _as_int(raw.get("routeIndex"))
        if idx is None:
            continue
        routes.append(
            ParsedRoute(
                route_index=idx,
                start_position=raw.get("startPosition"),
                end_position=raw.get("endPosition"),
                checkpoints=raw.get("checkpoints"),
            )
        )

    waves: list[ParsedWave] = []
    for raw in level_raw.get("waves", []):
        if not isinstance(raw, dict):
            continue
        w_idx = _as_int(raw.get("waveIndex"))
        if w_idx is None:
            continue
        spawns: list[ParsedSpawn] = []
        for fragment in raw.get("fragments", []):
            if not isinstance(fragment, dict):
                continue
            for action in fragment.get("actions", []):
                if not isinstance(action, dict):
                    continue
                enemy_id = action.get("enemyId")
                if not isinstance(enemy_id, str):
                    continue
                spawns.append(
                    ParsedSpawn(
                        enemy_game_id=enemy_id,
                        level_variant=_as_int(action.get("levelVariant")) or 0,
                        route_index=_as_int(action.get("routeIndex")),
                        spawn_time=_as_float(action.get("spawnTime")),
                        count=_as_int(action.get("count")),
                        interval=_as_float(action.get("interval")),
                        spawn_group=_as_str(action.get("spawnGroup")),
                        hidden=bool(action.get("hidden", False)),
                        source_fragment=action,
                    )
                )
        waves.append(
            ParsedWave(
                wave_index=w_idx,
                pre_delay=_as_float(raw.get("preDelay")),
                max_time_waiting=_as_float(raw.get("maxTimeWaiting")),
                spawns=spawns,
            )
        )

    return ParsedLevel(
        width=_as_int(map_data.get("width")),
        height=_as_int(map_data.get("height")),
        map_version=_as_str(map_data.get("mapVersion")),
        environment=map_data.get("environment"),
        tiles=tiles,
        routes=routes,
        waves=waves,
    )


@dataclass
class _SpawnAgg:
    total_count: int = 0
    first_spawn_time: float | None = None
    last_spawn_time: float | None = None
    route_pks: set[int] = field(default_factory=set)


def insert_level(
    conn: sqlite3.Connection,
    stage_pk: int,
    level: ParsedLevel,
    enemy_pk_by_game_id: dict[str, int],
) -> LevelImportResult:
    """Insert a stage's map/tiles/routes/waves/spawns and derive stage_enemies."""
    conn.execute(
        "INSERT INTO stage_maps (stage_pk, width, height, map_version, environment_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (stage_pk, level.width, level.height, level.map_version, _json_or_none(level.environment)),
    )

    for tile in level.tiles:
        conn.execute(
            "INSERT INTO stage_tiles "
            "(stage_pk, x, y, tile_key, height_type, buildable_type, passable, "
            "special_properties_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stage_pk,
                tile.x,
                tile.y,
                tile.tile_key,
                tile.height_type,
                tile.buildable_type,
                None if tile.passable is None else int(tile.passable),
                _json_or_none(tile.special_properties),
            ),
        )

    route_pk_by_index: dict[int, int] = {}
    for route in level.routes:
        cur = conn.execute(
            "INSERT INTO stage_routes "
            "(stage_pk, route_index, start_position_json, end_position_json, checkpoints_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                stage_pk,
                route.route_index,
                _json_or_none(route.start_position),
                _json_or_none(route.end_position),
                _json_or_none(route.checkpoints),
            ),
        )
        route_pk_by_index[route.route_index] = int(cur.lastrowid or 0)

    agg: dict[tuple[int, int], _SpawnAgg] = defaultdict(_SpawnAgg)
    spawns_inserted = 0
    for wave in level.waves:
        cur = conn.execute(
            "INSERT INTO stage_waves (stage_pk, wave_index, pre_delay, max_time_waiting) "
            "VALUES (?, ?, ?, ?)",
            (stage_pk, wave.wave_index, wave.pre_delay, wave.max_time_waiting),
        )
        wave_pk = int(cur.lastrowid or 0)
        for spawn in wave.spawns:
            enemy_pk = enemy_pk_by_game_id.get(spawn.enemy_game_id)
            if enemy_pk is None:
                raise ImporterError(
                    f"spawn references unknown enemy {spawn.enemy_game_id!r} in stage_pk={stage_pk}"
                )
            route_pk = (
                route_pk_by_index.get(spawn.route_index) if spawn.route_index is not None else None
            )
            conn.execute(
                "INSERT INTO stage_spawns "
                "(wave_pk, enemy_pk, enemy_level_variant, route_pk, spawn_time, count, "
                "interval, spawn_group, hidden_or_scripted, source_fragment_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    wave_pk,
                    enemy_pk,
                    spawn.level_variant,
                    route_pk,
                    spawn.spawn_time,
                    spawn.count,
                    spawn.interval,
                    spawn.spawn_group,
                    int(spawn.hidden),
                    _json_or_none(spawn.source_fragment),
                ),
            )
            spawns_inserted += 1

            bucket = agg[(enemy_pk, spawn.level_variant)]
            bucket.total_count += spawn.count or 0
            if spawn.spawn_time is not None:
                if bucket.first_spawn_time is None or spawn.spawn_time < bucket.first_spawn_time:
                    bucket.first_spawn_time = spawn.spawn_time
                if bucket.last_spawn_time is None or spawn.spawn_time > bucket.last_spawn_time:
                    bucket.last_spawn_time = spawn.spawn_time
            if route_pk is not None:
                bucket.route_pks.add(route_pk)

    for (enemy_pk, variant), bucket in sorted(agg.items()):
        conn.execute(
            "INSERT INTO stage_enemies "
            "(stage_pk, enemy_pk, enemy_level_variant, total_count, first_spawn_time, "
            "last_spawn_time, route_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                stage_pk,
                enemy_pk,
                variant,
                bucket.total_count,
                bucket.first_spawn_time,
                bucket.last_spawn_time,
                len(bucket.route_pks),
            ),
        )

    return LevelImportResult(
        tiles=len(level.tiles),
        routes=len(level.routes),
        waves=len(level.waves),
        spawns=spawns_inserted,
        stage_enemies=len(agg),
    )
