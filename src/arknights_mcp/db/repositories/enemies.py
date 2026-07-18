"""Enemy read repository (§V2; §T20/§T35).

Encapsulates the parameterized ``SELECT``s that back the ``get_enemy`` service: a
single enemy keyed by ``(server, game_id)`` -- the unique identity -- with its
region-scoped provenance joined in (§V5), plus the enemy's typed level variants
from ``enemy_levels`` (the stat block + allowlisted structural JSON fragments).
Rows are returned as flat, typed dataclasses that mirror the selected columns
1:1; domain shaping (JSON decode, envelope mapping) stays in the service.

The enemy join is on NOT NULL foreign keys
(``enemies -> record_provenance -> source_snapshots``), so a found enemy always
carries ``snapshot_id`` + ``imported_at`` (§V5). Every value is bound (§V2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arknights_mcp.db.repositories.base import Repository


@dataclass(frozen=True)
class EnemyRow:
    """One enemy row plus its joined region provenance (§V5)."""

    enemy_pk: int
    server: str
    game_id: str
    display_name: str | None
    enemy_class: str | None
    is_boss: bool
    is_elite: bool
    attack_type: str | None
    motion_type: str | None
    snapshot_id: str
    imported_at: str


@dataclass(frozen=True)
class EnemyLevelRow:
    """One level variant of an enemy (``enemy_levels``).

    Scalar stats are typed columns; ``targeting``/``immunities``/``abilities``
    stay JSON strings here (allowlisted + sanitized at import, §V18/§V31) and are
    decoded in the service.
    """

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
    targeting_json: str | None
    immunities_json: str | None
    abilities_json: str | None


_ENEMY_SQL = (
    "SELECT e.enemy_pk, e.server, e.game_id, e.display_name, e.enemy_class, "
    "e.is_boss, e.is_elite, e.attack_type, e.motion_type, "
    "p.snapshot_id, ss.imported_at "
    "FROM enemies e "
    "JOIN record_provenance p ON p.provenance_id = e.provenance_id "
    "JOIN source_snapshots ss ON ss.snapshot_id = p.snapshot_id "
    "WHERE e.server = ? AND e.game_id = ? "
    "LIMIT 1"
)

# Level variants ordered by variant so the emitted stat block is deterministic.
_LEVELS_SQL = (
    "SELECT level_variant, hp, atk, def, res, attack_interval, attack_range, "
    "move_speed, weight, life_point_reduction, block_behavior, "
    "targeting_json, immunities_json, abilities_json "
    "FROM enemy_levels WHERE enemy_pk = ? "
    "ORDER BY level_variant"
)


def _to_enemy_row(row: Any) -> EnemyRow:
    (
        enemy_pk,
        server,
        game_id,
        display_name,
        enemy_class,
        is_boss,
        is_elite,
        attack_type,
        motion_type,
        snapshot_id,
        imported_at,
    ) = row
    return EnemyRow(
        enemy_pk=enemy_pk,
        server=server,
        game_id=game_id,
        display_name=display_name,
        enemy_class=enemy_class,
        is_boss=bool(is_boss),
        is_elite=bool(is_elite),
        attack_type=attack_type,
        motion_type=motion_type,
        snapshot_id=snapshot_id,
        imported_at=imported_at,
    )


def _to_enemy_level_row(row: Any) -> EnemyLevelRow:
    (
        level_variant,
        hp,
        atk,
        def_,
        res,
        attack_interval,
        attack_range,
        move_speed,
        weight,
        life_point_reduction,
        block_behavior,
        targeting_json,
        immunities_json,
        abilities_json,
    ) = row
    return EnemyLevelRow(
        level_variant=level_variant,
        hp=hp,
        atk=atk,
        def_=def_,
        res=res,
        attack_interval=attack_interval,
        attack_range=attack_range,
        move_speed=move_speed,
        weight=weight,
        life_point_reduction=life_point_reduction,
        block_behavior=block_behavior,
        targeting_json=targeting_json,
        immunities_json=immunities_json,
        abilities_json=abilities_json,
    )


class EnemyRepository(Repository):
    """Read-only access to enemies and their level variants (§V2)."""

    def enemy_by_game_id(self, server: str, game_id: str) -> EnemyRow | None:
        """Enemy for ``(server, game_id)`` -- the unique key -- or ``None``."""
        row = self._one(_ENEMY_SQL, (server, game_id))
        return _to_enemy_row(row) if row is not None else None

    def enemy_levels(self, enemy_pk: int) -> list[EnemyLevelRow]:
        """Every level variant of the enemy, ordered by ``level_variant``."""
        return [_to_enemy_level_row(r) for r in self._all(_LEVELS_SQL, (enemy_pk,))]
