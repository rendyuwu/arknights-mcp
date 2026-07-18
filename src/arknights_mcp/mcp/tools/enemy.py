"""``get_enemy`` MCP tool (§T35; §V5/§V23; §I.tool).

Bridges the bounded :class:`~arknights_mcp.models.enemies.GetEnemyInput` (§T30) to
the shared :func:`~arknights_mcp.services.enemies.get_enemy` service (§V14) and
wraps the outcome in the typed
:class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope` (§T29). The tool owns no
query logic -- only the model -> service -> envelope mapping -- so both transports
dispatch identical read-only (§V2) behaviour from the single registry.

Two invariants are load-bearing here:

* **§V5** -- ``server`` is required, so every ``ok`` result is region-attributed +
  carries provenance (snapshot_id + imported_at); an ``en`` enemy is never
  surfaced under a ``cn`` query (the service resolves by the unique
  ``(server, game_id)`` key), so en/cn are never silently mixed.
* **§V23** -- every result is a typed-status envelope (``ok``/``not_found``); a
  database failure or any unexpected error fails closed to a fixed, path/trace-free
  envelope via the shared :func:`~arknights_mcp.mcp.tools._shared.run_guarded`
  guard (``database_unavailable``/``internal_error``).
"""

from __future__ import annotations

from arknights_mcp.mcp.envelopes import Provenance, ResponseEnvelope, error, ok
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._shared import ConnectionProvider, run_guarded
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.enemies import GetEnemyInput
from arknights_mcp.services.enemies import (
    EnemyDetailResult,
    EnemyFacts,
    EnemyLevelFacts,
    get_enemy,
)

_TOOL_NAME = "get_enemy"
_TOOL_TITLE = "Get enemy"
_TOOL_DESCRIPTION = (
    "Fetch one Arknights enemy's facts by region + game_id: class, boss/elite "
    "flags, attack/motion type, and the per-level stat block (hp, atk, def, res, "
    "attack interval/range, move speed, weight, life-point reduction) with "
    "immunities and abilities. en/cn are never mixed."
)

_NOT_FOUND_MESSAGE = "no enemy matched the given region and game_id"
_NOT_FOUND_ACTION = (
    "verify the server and game_id (use search_entities to find it), or run "
    "`arknights-mcp status` to check the active build"
)


def _level_to_dict(level: EnemyLevelFacts) -> dict[str, object]:
    """One level variant's typed stat block (structural JSON already vetted; §V18)."""
    return {
        "level_variant": level.level_variant,
        "hp": level.hp,
        "atk": level.atk,
        "def": level.def_,
        "res": level.res,
        "attack_interval": level.attack_interval,
        "attack_range": level.attack_range,
        "move_speed": level.move_speed,
        "weight": level.weight,
        "life_point_reduction": level.life_point_reduction,
        "block_behavior": level.block_behavior,
        "targeting": level.targeting,
        "immunities": level.immunities,
        "abilities": level.abilities,
    }


def _enemy_to_dict(enemy: EnemyFacts) -> dict[str, object]:
    """The typed enemy facts + ordered level variants (no prose; §V16/§V18)."""
    return {
        "server": enemy.server,
        "game_id": enemy.game_id,
        "display_name": enemy.display_name,
        "enemy_class": enemy.enemy_class,
        "is_boss": enemy.is_boss,
        "is_elite": enemy.is_elite,
        "attack_type": enemy.attack_type,
        "motion_type": enemy.motion_type,
        "levels": [_level_to_dict(level) for level in enemy.levels],
    }


def _shape(result: EnemyDetailResult) -> ResponseEnvelope:
    """Map the domain result to a typed §V23 envelope (§V5 region + provenance)."""
    if result.status == "not_found" or result.enemy is None:
        return error("not_found", _NOT_FOUND_MESSAGE, suggested_action=_NOT_FOUND_ACTION)

    prov = result.enemy.provenance
    return ok(
        {"enemy": _enemy_to_dict(result.enemy)},
        provenance=[
            Provenance(
                server=result.enemy.server,
                snapshot_id=prov.snapshot_id,
                imported_at=prov.imported_at,
            )
        ],
    )


def build_get_enemy_spec(get_conn: ConnectionProvider) -> ToolSpec:
    """Build the ``get_enemy`` :class:`ToolSpec` (§T35; §V14).

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build. The returned spec is read-only (§V2) for the single shared registry
    both transports dispatch from (§V14); its ``input_schema`` is the bounded
    model's JSON Schema, so the §V5 required ``server`` + §V18 ``game_id`` cap land
    on the wire exactly as validated.
    """

    def handler(**params: object) -> ResponseEnvelope:
        # §V5/§V18 gate: the bounded model requires a region, caps the game_id
        # length, and rejects an unknown parameter *before* any query runs -- a
        # ValidationError propagates as a protocol-level rejection.
        parsed = GetEnemyInput.model_validate(params)
        return run_guarded(
            get_conn,
            lambda conn: get_enemy(conn, server=parsed.server, game_id=parsed.game_id),
            _shape,
        )

    return ToolSpec(
        name=_TOOL_NAME,
        title=_TOOL_TITLE,
        description=_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(GetEnemyInput),
    )
