"""Stage read repository (§V2; §T20).

Encapsulates the parameterized ``SELECT``s that back the ``analyze_stage``
service: a single stage keyed by ``(server, game_id | stage_code)`` with its
region-scoped provenance joined in (§V5), and the stage's typed enemy
occurrences from the derived ``stage_enemies`` summary (§V6/§V26 inputs). Rows
are returned as flat, typed dataclasses that mirror the selected columns 1:1;
domain shaping (analyzer inputs, ability parsing) stays in the service.

The two stage joins are on NOT NULL foreign keys
(``stages -> record_provenance -> source_snapshots``), so an "ok" stage always
carries ``snapshot_id`` + ``imported_at`` (§V5). Every value is bound (§V2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arknights_mcp.db.repositories.base import Repository


@dataclass(frozen=True)
class StageRow:
    """One stage row plus its joined region provenance (§V5)."""

    stage_pk: int
    server: str
    game_id: str
    stage_code: str | None
    display_name: str | None
    zone_game_id: str | None
    stage_type: str | None
    difficulty: str | None
    sanity_cost: int | None
    recommended_level: int | None
    max_life_points: int | None
    snapshot_id: str
    imported_at: str


@dataclass(frozen=True)
class StageEnemyRow:
    """One enemy's typed occurrence in a stage (from ``stage_enemies``).

    Carries the M3 analyzer stat inputs (``def_`` / ``res`` / ``attack_range`` /
    ``block_behavior``) joined from the matching ``enemy_levels`` variant (§T39);
    they are ``None`` when the level row or the source field is absent (§V26).
    """

    game_id: str
    display_name: str | None
    enemy_class: str | None
    is_boss: bool
    is_elite: bool
    motion_type: str | None
    attack_type: str | None
    level_variant: int
    total_count: int | None
    first_spawn_time: float | None
    last_spawn_time: float | None
    route_count: int | None
    abilities_json: str | None
    def_: int | None
    res: int | None
    attack_range: float | None
    block_behavior: str | None


@dataclass(frozen=True)
class StageMapRow:
    """The stage's map header (``stage_maps``); tiles are paged separately."""

    width: int | None
    height: int | None
    map_version: str | None
    environment_json: str | None


@dataclass(frozen=True)
class StageTileRow:
    """One tile of the stage grid (``stage_tiles``)."""

    x: int
    y: int
    tile_key: str | None
    height_type: str | None
    buildable_type: str | None
    passable: bool | None


@dataclass(frozen=True)
class StageRouteRow:
    """One enemy route (``stage_routes``); position payloads stay JSON strings."""

    route_index: int
    start_position_json: str | None
    end_position_json: str | None
    checkpoints_json: str | None


@dataclass(frozen=True)
class StageSpawnRow:
    """One scheduled spawn (``stage_spawns``) joined to its wave + enemy + route."""

    wave_index: int
    enemy_game_id: str
    enemy_level_variant: int | None
    route_index: int | None
    spawn_time: float | None
    count: int | None
    interval: float | None
    spawn_group: str | None
    hidden: bool


_STAGE_SELECT = (
    "SELECT s.stage_pk, s.server, s.game_id, s.stage_code, s.display_name, "
    "z.game_id, s.stage_type, s.difficulty, s.sanity_cost, "
    "s.recommended_level, s.max_life_points, p.snapshot_id, ss.imported_at "
    "FROM stages s "
    "JOIN record_provenance p ON p.provenance_id = s.provenance_id "
    "JOIN source_snapshots ss ON ss.snapshot_id = p.snapshot_id "
    "LEFT JOIN zones z ON z.zone_pk = s.zone_pk "
    "WHERE s.server = ? AND "
)
_STAGE_BY_CODE_SQL = _STAGE_SELECT + "s.stage_code = ? ORDER BY s.stage_pk LIMIT 1"
_STAGE_BY_GAME_ID_SQL = _STAGE_SELECT + "s.game_id = ? ORDER BY s.stage_pk LIMIT 1"

_OCCURRENCES_SQL = (
    "SELECT e.game_id, e.display_name, e.enemy_class, e.is_boss, e.is_elite, "
    "e.motion_type, e.attack_type, se.enemy_level_variant, se.total_count, "
    "se.first_spawn_time, se.last_spawn_time, se.route_count, el.abilities_json, "
    'el."def", el.res, el.attack_range, el.block_behavior '
    "FROM stage_enemies se "
    "JOIN enemies e ON e.enemy_pk = se.enemy_pk "
    "LEFT JOIN enemy_levels el "
    "ON el.enemy_pk = se.enemy_pk AND el.level_variant = se.enemy_level_variant "
    "WHERE se.stage_pk = ? "
    "ORDER BY e.game_id, se.enemy_level_variant"
)

# Deploy-surface tile counts for the tiles/deploy rule (§T39): a buildable LOWLAND
# tile holds a melee unit, a buildable HIGHLAND tile a ranged unit; a NONE/absent
# ``buildable_type`` is not deployable. SUM over zero rows is NULL (coalesced to 0).
_TILE_SUMMARY_SQL = (
    "SELECT COUNT(*), "
    "SUM(CASE WHEN UPPER(COALESCE(buildable_type, 'NONE')) NOT IN ('NONE', '') "
    "AND UPPER(COALESCE(height_type, '')) = 'LOWLAND' THEN 1 ELSE 0 END), "
    "SUM(CASE WHEN UPPER(COALESCE(buildable_type, 'NONE')) NOT IN ('NONE', '') "
    "AND UPPER(COALESCE(height_type, '')) = 'HIGHLAND' THEN 1 ELSE 0 END) "
    "FROM stage_tiles WHERE stage_pk = ?"
)

# --- get_stage opt-in sections (§T34): each paged through a bounded LIMIT/OFFSET
# with a deterministic ORDER BY so a client can page without ever pulling an
# unbounded slice (§V19), and repeat a page reproducibly.
_MAP_SQL = "SELECT width, height, map_version, environment_json FROM stage_maps WHERE stage_pk = ?"

_TILE_COUNT_SQL = "SELECT COUNT(*) FROM stage_tiles WHERE stage_pk = ?"
_TILES_SQL = (
    "SELECT x, y, tile_key, height_type, buildable_type, passable "
    "FROM stage_tiles WHERE stage_pk = ? "
    "ORDER BY y, x LIMIT ? OFFSET ?"
)

_ROUTE_COUNT_SQL = "SELECT COUNT(*) FROM stage_routes WHERE stage_pk = ?"
_ROUTES_SQL = (
    "SELECT route_index, start_position_json, end_position_json, checkpoints_json "
    "FROM stage_routes WHERE stage_pk = ? "
    "ORDER BY route_index LIMIT ? OFFSET ?"
)

# Mirror the JOINs of ``_SPAWNS_SQL`` exactly (incl. the enemies inner join) so
# the count matches the number of rows the page query can return: a spawn whose
# enemy row is absent is dropped by both, keeping ``total``/``has_more`` honest.
_SPAWN_COUNT_SQL = (
    "SELECT COUNT(*) FROM stage_spawns sp "
    "JOIN stage_waves w ON w.wave_pk = sp.wave_pk "
    "JOIN enemies e ON e.enemy_pk = sp.enemy_pk "
    "WHERE w.stage_pk = ?"
)
_SPAWNS_SQL = (
    "SELECT w.wave_index, e.game_id, sp.enemy_level_variant, r.route_index, "
    "sp.spawn_time, sp.count, sp.interval, sp.spawn_group, sp.hidden_or_scripted "
    "FROM stage_spawns sp "
    "JOIN stage_waves w ON w.wave_pk = sp.wave_pk "
    "JOIN enemies e ON e.enemy_pk = sp.enemy_pk "
    "LEFT JOIN stage_routes r ON r.route_pk = sp.route_pk "
    "WHERE w.stage_pk = ? "
    "ORDER BY w.wave_index, sp.spawn_time, e.game_id, sp.spawn_pk "
    "LIMIT ? OFFSET ?"
)


def _to_stage_row(row: Any) -> StageRow:
    (
        stage_pk,
        server,
        game_id,
        stage_code,
        display_name,
        zone_game_id,
        stage_type,
        difficulty,
        sanity_cost,
        recommended_level,
        max_life_points,
        snapshot_id,
        imported_at,
    ) = row
    return StageRow(
        stage_pk=stage_pk,
        server=server,
        game_id=game_id,
        stage_code=stage_code,
        display_name=display_name,
        zone_game_id=zone_game_id,
        stage_type=stage_type,
        difficulty=difficulty,
        sanity_cost=sanity_cost,
        recommended_level=recommended_level,
        max_life_points=max_life_points,
        snapshot_id=snapshot_id,
        imported_at=imported_at,
    )


def _to_stage_enemy_row(row: Any) -> StageEnemyRow:
    (
        game_id,
        display_name,
        enemy_class,
        is_boss,
        is_elite,
        motion_type,
        attack_type,
        level_variant,
        total_count,
        first_spawn_time,
        last_spawn_time,
        route_count,
        abilities_json,
        def_,
        res,
        attack_range,
        block_behavior,
    ) = row
    return StageEnemyRow(
        game_id=game_id,
        display_name=display_name,
        enemy_class=enemy_class,
        is_boss=bool(is_boss),
        is_elite=bool(is_elite),
        motion_type=motion_type,
        attack_type=attack_type,
        level_variant=level_variant,
        total_count=total_count,
        first_spawn_time=first_spawn_time,
        last_spawn_time=last_spawn_time,
        route_count=route_count,
        abilities_json=abilities_json,
        def_=def_,
        res=res,
        attack_range=attack_range,
        block_behavior=block_behavior,
    )


def _to_stage_map_row(row: Any) -> StageMapRow:
    width, height, map_version, environment_json = row
    return StageMapRow(
        width=width,
        height=height,
        map_version=map_version,
        environment_json=environment_json,
    )


def _to_stage_tile_row(row: Any) -> StageTileRow:
    x, y, tile_key, height_type, buildable_type, passable = row
    return StageTileRow(
        x=x,
        y=y,
        tile_key=tile_key,
        height_type=height_type,
        buildable_type=buildable_type,
        passable=None if passable is None else bool(passable),
    )


def _to_stage_route_row(row: Any) -> StageRouteRow:
    route_index, start_position_json, end_position_json, checkpoints_json = row
    return StageRouteRow(
        route_index=route_index,
        start_position_json=start_position_json,
        end_position_json=end_position_json,
        checkpoints_json=checkpoints_json,
    )


def _to_stage_spawn_row(row: Any) -> StageSpawnRow:
    (
        wave_index,
        enemy_game_id,
        enemy_level_variant,
        route_index,
        spawn_time,
        count,
        interval,
        spawn_group,
        hidden,
    ) = row
    return StageSpawnRow(
        wave_index=wave_index,
        enemy_game_id=enemy_game_id,
        enemy_level_variant=enemy_level_variant,
        route_index=route_index,
        spawn_time=spawn_time,
        count=count,
        interval=interval,
        spawn_group=spawn_group,
        hidden=bool(hidden),
    )


class StageRepository(Repository):
    """Read-only access to stages and their enemy occurrences (§V2)."""

    def stage_by_game_id(self, server: str, game_id: str) -> StageRow | None:
        """Stage for ``(server, game_id)`` -- the unique key -- or ``None``."""
        row = self._one(_STAGE_BY_GAME_ID_SQL, (server, game_id))
        return _to_stage_row(row) if row is not None else None

    def stage_by_code(self, server: str, stage_code: str) -> StageRow | None:
        """Stage for ``(server, stage_code)`` or ``None`` (first by ``stage_pk``)."""
        row = self._one(_STAGE_BY_CODE_SQL, (server, stage_code))
        return _to_stage_row(row) if row is not None else None

    def stage_enemies(self, stage_pk: int) -> list[StageEnemyRow]:
        """Every enemy occurrence in the stage, ordered by ``game_id`` then variant."""
        return [_to_stage_enemy_row(r) for r in self._all(_OCCURRENCES_SQL, (stage_pk,))]

    def tile_summary(self, stage_pk: int) -> tuple[int, int, int]:
        """Deploy-surface tile counts ``(total, buildable_melee, buildable_ranged)``
        for the tiles/deploy rule (§T39). Melee = buildable LOWLAND, ranged =
        buildable HIGHLAND; a ``total`` of 0 means the stage has no tile rows."""
        total, melee, ranged = self._one(_TILE_SUMMARY_SQL, (stage_pk,))
        return int(total), int(melee or 0), int(ranged or 0)

    # --- get_stage opt-in sections (§T34): map header + paged tiles/routes/spawns.

    def stage_map(self, stage_pk: int) -> StageMapRow | None:
        """The stage's map header (``stage_maps``) or ``None`` if absent."""
        row = self._one(_MAP_SQL, (stage_pk,))
        return _to_stage_map_row(row) if row is not None else None

    def tile_count(self, stage_pk: int) -> int:
        """Total tiles in the stage grid (for the §V19 page descriptor)."""
        return int(self._one(_TILE_COUNT_SQL, (stage_pk,))[0])

    def tiles(self, stage_pk: int, limit: int, offset: int) -> list[StageTileRow]:
        """One bounded page of tiles, ordered ``(y, x)`` for deterministic paging."""
        return [_to_stage_tile_row(r) for r in self._all(_TILES_SQL, (stage_pk, limit, offset))]

    def route_count(self, stage_pk: int) -> int:
        """Total routes in the stage (for the §V19 page descriptor)."""
        return int(self._one(_ROUTE_COUNT_SQL, (stage_pk,))[0])

    def routes(self, stage_pk: int, limit: int, offset: int) -> list[StageRouteRow]:
        """One bounded page of routes, ordered by ``route_index``."""
        return [_to_stage_route_row(r) for r in self._all(_ROUTES_SQL, (stage_pk, limit, offset))]

    def spawn_count(self, stage_pk: int) -> int:
        """Total scheduled spawns in the stage (for the §V19 page descriptor)."""
        return int(self._one(_SPAWN_COUNT_SQL, (stage_pk,))[0])

    def spawns(self, stage_pk: int, limit: int, offset: int) -> list[StageSpawnRow]:
        """One bounded page of spawns, ordered ``(wave, spawn_time, enemy, spawn_pk)``."""
        return [_to_stage_spawn_row(r) for r in self._all(_SPAWNS_SQL, (stage_pk, limit, offset))]
