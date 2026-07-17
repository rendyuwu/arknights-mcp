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
    """One enemy's typed occurrence in a stage (from ``stage_enemies``)."""

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
    "se.first_spawn_time, se.last_spawn_time, se.route_count, el.abilities_json "
    "FROM stage_enemies se "
    "JOIN enemies e ON e.enemy_pk = se.enemy_pk "
    "LEFT JOIN enemy_levels el "
    "ON el.enemy_pk = se.enemy_pk AND el.level_variant = se.enemy_level_variant "
    "WHERE se.stage_pk = ? "
    "ORDER BY e.game_id, se.enemy_level_variant"
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
