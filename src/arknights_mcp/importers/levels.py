"""Level parser: map / tiles / routes / waves / spawns + derived stage_enemies.

Reads the allowlisted structural fields of a stage's level file (§V18 — known
keys read explicitly, string fields sanitized, no prose) and writes the map,
tiles, routes, waves, and spawns for one stage, then derives the
``stage_enemies`` summary. Cross-file references (spawn -> enemy) must resolve
(§21.2); an unresolved enemy fails closed.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.field_policy import (
    SPAWN_ACTION_ALLOWLIST,
    apply_allowlist,
    sanitize_value,
)
from arknights_mcp.util.coerce import as_float, as_int, as_str, json_or_none
from arknights_mcp.util.sqlite import integrity_guard


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
class ParsedVariant:
    """A stage-scoped inline enemy variant (``useDb:false`` ref; §T80/§V43).

    ``prefab_base_game_id`` is the base enemy it derives from (resolved to an
    ``enemy_pk`` FK at insert). Stat fields are the §V29-verified overrides
    ``overwrittenData`` defined; ``None`` = not overridden, so the base value is
    inherited at read. ``source_fragment`` is the allowlisted, prose-free trace.
    """

    variant_id: str
    prefab_base_game_id: str
    hp: int | None
    atk: int | None
    def_: int | None
    res: int | None
    attack_interval: float | None
    move_speed: float | None
    weight: int | None
    life_point_reduction: int | None
    motion_type: str | None
    source_fragment: dict[str, Any]


@dataclass(frozen=True)
class ParsedLevel:
    width: int | None
    height: int | None
    map_version: str | None
    environment: Any
    tiles: list[ParsedTile]
    routes: list[ParsedRoute]
    waves: list[ParsedWave]
    variants: list[ParsedVariant]


@dataclass(frozen=True)
class LevelImportResult:
    tiles: int = 0
    routes: int = 0
    waves: int = 0
    spawns: int = 0
    stage_enemies: int = 0
    variants: int = 0


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
        x, y = as_int(raw.get("x")), as_int(raw.get("y"))
        if x is None or y is None:
            continue
        passable = raw.get("passable")
        tiles.append(
            ParsedTile(
                x=x,
                y=y,
                tile_key=as_str(raw.get("tileKey"), sanitize=True),
                height_type=as_str(raw.get("heightType"), sanitize=True),
                buildable_type=as_str(raw.get("buildableType"), sanitize=True),
                passable=bool(passable) if isinstance(passable, bool) else None,
                special_properties=sanitize_value(raw.get("specialProperties")),
            )
        )

    routes: list[ParsedRoute] = []
    for raw in level_raw.get("routes", []):
        if not isinstance(raw, dict):
            continue
        idx = as_int(raw.get("routeIndex"))
        if idx is None:
            continue
        routes.append(
            ParsedRoute(
                route_index=idx,
                start_position=sanitize_value(raw.get("startPosition")),
                end_position=sanitize_value(raw.get("endPosition")),
                checkpoints=sanitize_value(raw.get("checkpoints")),
            )
        )

    waves: list[ParsedWave] = []
    for raw in level_raw.get("waves", []):
        if not isinstance(raw, dict):
            continue
        w_idx = as_int(raw.get("waveIndex"))
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
                        level_variant=as_int(action.get("levelVariant")) or 0,
                        route_index=as_int(action.get("routeIndex")),
                        spawn_time=as_float(action.get("spawnTime")),
                        count=as_int(action.get("count")),
                        interval=as_float(action.get("interval")),
                        spawn_group=as_str(action.get("spawnGroup"), sanitize=True),
                        hidden=bool(action.get("hidden", False)),
                        # V18: keep only the allowlisted structural spawn fields
                        # (sanitized), never the whole raw action (which may carry
                        # prose/injection fields).
                        source_fragment=apply_allowlist(action, SPAWN_ACTION_ALLOWLIST).kept,
                    )
                )
        waves.append(
            ParsedWave(
                wave_index=w_idx,
                pre_delay=as_float(raw.get("preDelay")),
                max_time_waiting=as_float(raw.get("maxTimeWaiting")),
                spawns=spawns,
            )
        )

    variants: list[ParsedVariant] = []
    for raw in level_raw.get("variants", []):
        if not isinstance(raw, dict):
            continue
        variant_id = as_str(raw.get("variantId"), sanitize=True)
        prefab = as_str(raw.get("prefabKey"), sanitize=True)
        if not variant_id or not prefab:
            continue
        variants.append(
            ParsedVariant(
                variant_id=variant_id,
                prefab_base_game_id=prefab,
                hp=as_int(raw.get("hp")),
                atk=as_int(raw.get("atk")),
                def_=as_int(raw.get("def")),
                res=as_int(raw.get("res")),
                attack_interval=as_float(raw.get("attackInterval")),
                move_speed=as_float(raw.get("moveSpeed")),
                weight=as_int(raw.get("weight")),
                life_point_reduction=as_int(raw.get("lifePointReduction")),
                motion_type=as_str(raw.get("motion"), sanitize=True),
                # Prose-free trace: the inline id + its base prefab (both id-charset
                # strings). The overriding stats live in the typed columns above.
                source_fragment={"variantId": variant_id, "prefabKey": prefab},
            )
        )

    return ParsedLevel(
        width=as_int(map_data.get("width")),
        height=as_int(map_data.get("height")),
        map_version=as_str(map_data.get("mapVersion"), sanitize=True),
        environment=sanitize_value(map_data.get("environment")),
        tiles=tiles,
        routes=routes,
        waves=waves,
        variants=variants,
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
    *,
    provenance_id: int,
) -> LevelImportResult:
    """Insert a stage's map/tiles/routes/waves/spawns and derive stage_enemies.

    Every level-derived row carries ``provenance_id`` (§V17): all rows originate
    from the same level file, whose provenance is created once by the caller. A
    duplicate/absent structural index (a UNIQUE/PK collision) is surfaced as a
    graceful :class:`ImporterError` rather than an uncaught ``IntegrityError`` that
    would tear down the whole candidate build with a raw traceback (§V3).
    """
    with integrity_guard(
        f"level import for stage_pk={stage_pk} violates a uniqueness constraint "
        f"(duplicate or missing structural index)",
        ImporterError,
    ):
        return _insert_level(conn, stage_pk, level, enemy_pk_by_game_id, provenance_id)


def _insert_level(
    conn: sqlite3.Connection,
    stage_pk: int,
    level: ParsedLevel,
    enemy_pk_by_game_id: dict[str, int],
    provenance_id: int,
) -> LevelImportResult:
    conn.execute(
        "INSERT INTO stage_maps "
        "(stage_pk, width, height, map_version, environment_json, provenance_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            stage_pk,
            level.width,
            level.height,
            level.map_version,
            json_or_none(level.environment),
            provenance_id,
        ),
    )

    for tile in level.tiles:
        conn.execute(
            "INSERT INTO stage_tiles "
            "(stage_pk, x, y, tile_key, height_type, buildable_type, passable, "
            "special_properties_json, provenance_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stage_pk,
                tile.x,
                tile.y,
                tile.tile_key,
                tile.height_type,
                tile.buildable_type,
                None if tile.passable is None else int(tile.passable),
                json_or_none(tile.special_properties),
                provenance_id,
            ),
        )

    route_pk_by_index: dict[int, int] = {}
    for route in level.routes:
        cur = conn.execute(
            "INSERT INTO stage_routes "
            "(stage_pk, route_index, start_position_json, end_position_json, checkpoints_json, "
            "provenance_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                stage_pk,
                route.route_index,
                json_or_none(route.start_position),
                json_or_none(route.end_position),
                json_or_none(route.checkpoints),
                provenance_id,
            ),
        )
        route_pk_by_index[route.route_index] = int(cur.lastrowid or 0)

    # Stage-scoped inline enemy variants (§T80/§V43). Each resolves its base prefab
    # to an enemy_pk (the FK); a base absent from the region's enemies fails closed
    # (§V3, never a fabricated enemy row). Insert before spawns so a spawn/occurrence
    # can carry its variant_pk.
    variant_pk_by_id: dict[str, int] = {}
    for variant in level.variants:
        base_pk = enemy_pk_by_game_id.get(variant.prefab_base_game_id)
        if base_pk is None:
            raise ImporterError(
                f"inline variant {variant.variant_id!r} references unknown base enemy "
                f"{variant.prefab_base_game_id!r} in stage_pk={stage_pk}"
            )
        cur = conn.execute(
            "INSERT INTO stage_enemy_variants "
            "(stage_pk, variant_id, prefab_base_enemy_pk, hp, atk, def, res, attack_interval, "
            "move_speed, weight, life_point_reduction, motion_type, source_fragment_json, "
            "provenance_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stage_pk,
                variant.variant_id,
                base_pk,
                variant.hp,
                variant.atk,
                variant.def_,
                variant.res,
                variant.attack_interval,
                variant.move_speed,
                variant.weight,
                variant.life_point_reduction,
                variant.motion_type,
                json_or_none(variant.source_fragment),
                provenance_id,
            ),
        )
        variant_pk_by_id[variant.variant_id] = int(cur.lastrowid or 0)

    # Occurrence identity includes variant_pk so two distinct inline variants of the
    # same base prefab + level do not collapse (each keeps its own overridden stats).
    agg: dict[tuple[int, int, int | None], _SpawnAgg] = defaultdict(_SpawnAgg)
    spawns_inserted = 0
    for wave in level.waves:
        cur = conn.execute(
            "INSERT INTO stage_waves "
            "(stage_pk, wave_index, pre_delay, max_time_waiting, provenance_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (stage_pk, wave.wave_index, wave.pre_delay, wave.max_time_waiting, provenance_id),
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
            # A useDb:false inline-variant spawn carries the inline id under
            # ``variantId``; link it to the stage-scoped variant row so reads overlay
            # its stats over the base prefab (§T80). A useDb:true spawn has none.
            spawn_variant_id = spawn.source_fragment.get("variantId")
            variant_pk = (
                variant_pk_by_id.get(spawn_variant_id)
                if isinstance(spawn_variant_id, str)
                else None
            )
            conn.execute(
                "INSERT INTO stage_spawns "
                "(wave_pk, enemy_pk, enemy_level_variant, variant_pk, route_pk, spawn_time, count, "
                "interval, spawn_group, hidden_or_scripted, source_fragment_json, provenance_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    wave_pk,
                    enemy_pk,
                    spawn.level_variant,
                    variant_pk,
                    route_pk,
                    spawn.spawn_time,
                    spawn.count,
                    spawn.interval,
                    spawn.spawn_group,
                    int(spawn.hidden),
                    json_or_none(spawn.source_fragment),
                    provenance_id,
                ),
            )
            spawns_inserted += 1

            bucket = agg[(enemy_pk, spawn.level_variant, variant_pk)]
            bucket.total_count += spawn.count or 0
            if spawn.spawn_time is not None:
                if bucket.first_spawn_time is None or spawn.spawn_time < bucket.first_spawn_time:
                    bucket.first_spawn_time = spawn.spawn_time
                if bucket.last_spawn_time is None or spawn.spawn_time > bucket.last_spawn_time:
                    bucket.last_spawn_time = spawn.spawn_time
            if route_pk is not None:
                bucket.route_pks.add(route_pk)

    # Sort with a NULL-safe key: a base-enemy occurrence has variant_pk None, which
    # cannot be ordered against an int; treat it as -1 so ordering stays total.
    for (enemy_pk, level_variant, variant_pk), bucket in sorted(
        agg.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] if kv[0][2] is not None else -1)
    ):
        conn.execute(
            "INSERT INTO stage_enemies "
            "(stage_pk, enemy_pk, enemy_level_variant, variant_pk, total_count, first_spawn_time, "
            "last_spawn_time, route_count, provenance_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stage_pk,
                enemy_pk,
                level_variant,
                variant_pk,
                bucket.total_count,
                bucket.first_spawn_time,
                bucket.last_spawn_time,
                len(bucket.route_pks),
                provenance_id,
            ),
        )

    return LevelImportResult(
        tiles=len(level.tiles),
        routes=len(level.routes),
        waves=len(level.waves),
        spawns=spawns_inserted,
        stage_enemies=len(agg),
        variants=len(level.variants),
    )
