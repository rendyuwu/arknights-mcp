"""``get_stage`` MCP tool (§T34; §V19/§V22/§V23; §I.tool).

Bridges the bounded :class:`~arknights_mcp.models.stages.GetStageInput` (§T30) to
the shared :func:`~arknights_mcp.services.stages.get_stage` service (§V14) and
wraps the outcome in the typed
:class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope` (§T29). The tool owns no
query logic -- only the model -> service -> envelope mapping -- so both transports
dispatch identical read-only (§V2) behaviour from the single registry.

Two invariants are load-bearing here:

* **§V22** -- the default response is stage facts + provenance only. The heavy
  ``map`` (tile grid), ``routes`` and ``spawns`` sections are opt-in include flags
  (default off); the envelope's size cap fails closed on any oversized payload.
* **§V19** -- each opted-in section is a *bounded page*: the input model rejects an
  out-of-range ``page_size`` (> 100) before the handler runs, the service rejects
  it again, and a section carries a ``*_page`` descriptor (``has_more``) so a
  client pages deterministically instead of pulling an unbounded slice.

Every ``ok`` result carries region + provenance (§V5); a database failure or any
unexpected error fails closed to a fixed, path/trace-free envelope (§V23) via the
shared :func:`~arknights_mcp.mcp.tools._shared.run_guarded` guard.
"""

from __future__ import annotations

from arknights_mcp.mcp.envelopes import Provenance, ResponseEnvelope, error, ok
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._shared import ConnectionProvider, run_guarded
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.stages import GetStageInput
from arknights_mcp.services.stages import (
    RouteFacts,
    SectionPage,
    SpawnFacts,
    StageDetailResult,
    StageFacts,
    StageMapFacts,
    TileFacts,
    get_stage,
)

_TOOL_NAME = "get_stage"
_TOOL_TITLE = "Get stage"
_TOOL_DESCRIPTION = (
    "Fetch one Arknights stage's facts by region + stage_code (e.g. 4-4) or "
    "game_id. The default response is compact facts + provenance; set include_map "
    "/ include_routes / include_spawns to add the (paged) tile grid, enemy routes, "
    "or spawn timeline. en/cn are never mixed."
)

_NOT_FOUND_MESSAGE = "no stage matched the given region and selector"
_NOT_FOUND_ACTION = (
    "verify the server and stage_code/game_id, or run `arknights-mcp status` "
    "to check the active build"
)


def _stage_to_dict(stage: StageFacts) -> dict[str, object]:
    """The compact, always-present stage facts (no prose; §V16/§V18)."""
    return {
        "server": stage.server,
        "game_id": stage.game_id,
        "stage_code": stage.stage_code,
        "display_name": stage.display_name,
        "zone_game_id": stage.zone_game_id,
        "stage_type": stage.stage_type,
        "difficulty": stage.difficulty,
        "sanity_cost": stage.sanity_cost,
        "recommended_level": stage.recommended_level,
        "max_life_points": stage.max_life_points,
    }


def _page_to_dict(page: SectionPage) -> dict[str, object]:
    """The §V19 page descriptor -- ``has_more`` signals another bounded page."""
    return {
        "page": page.page,
        "page_size": page.page_size,
        "total": page.total,
        "has_more": page.has_more,
    }


def _tile_to_dict(tile: TileFacts) -> dict[str, object]:
    return {
        "x": tile.x,
        "y": tile.y,
        "tile_key": tile.tile_key,
        "height_type": tile.height_type,
        "buildable_type": tile.buildable_type,
        "passable": tile.passable,
    }


def _map_header_to_dict(stage_map: StageMapFacts) -> dict[str, object]:
    """The map header only. Tiles ride as their own top-level paged section, so
    all three heavy sections share one shape (list + ``*_page``)."""
    return {
        "width": stage_map.width,
        "height": stage_map.height,
        "map_version": stage_map.map_version,
        "environment": stage_map.environment,
    }


def _route_to_dict(route: RouteFacts) -> dict[str, object]:
    return {
        "route_index": route.route_index,
        "start_position": route.start_position,
        "end_position": route.end_position,
        "checkpoints": route.checkpoints,
    }


def _spawn_to_dict(spawn: SpawnFacts) -> dict[str, object]:
    return {
        "wave_index": spawn.wave_index,
        "enemy_game_id": spawn.enemy_game_id,
        "enemy_level_variant": spawn.enemy_level_variant,
        "route_index": spawn.route_index,
        "spawn_time": spawn.spawn_time,
        "count": spawn.count,
        "interval": spawn.interval,
        "spawn_group": spawn.spawn_group,
        "hidden": spawn.hidden,
    }


def _shape(result: StageDetailResult) -> ResponseEnvelope:
    """Map the domain result to a typed §V23 envelope (§V5 region + provenance)."""
    if result.status == "not_found" or result.stage is None:
        return error("not_found", _NOT_FOUND_MESSAGE, suggested_action=_NOT_FOUND_ACTION)

    data: dict[str, object] = {"stage": _stage_to_dict(result.stage)}
    if result.stage_map is not None and result.tiles_page is not None:
        data["map"] = _map_header_to_dict(result.stage_map)
        data["tiles"] = [_tile_to_dict(t) for t in result.tiles]
        data["tiles_page"] = _page_to_dict(result.tiles_page)
    if result.routes_page is not None:
        data["routes"] = [_route_to_dict(r) for r in result.routes]
        data["routes_page"] = _page_to_dict(result.routes_page)
    if result.spawns_page is not None:
        data["spawns"] = [_spawn_to_dict(s) for s in result.spawns]
        data["spawns_page"] = _page_to_dict(result.spawns_page)

    prov = result.stage.provenance
    return ok(
        data,
        provenance=[
            Provenance(
                server=result.stage.server,
                snapshot_id=prov.snapshot_id,
                imported_at=prov.imported_at,
            )
        ],
    )


def build_get_stage_spec(get_conn: ConnectionProvider) -> ToolSpec:
    """Build the ``get_stage`` :class:`ToolSpec` (§T34; §V14).

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build. The returned spec is read-only (§V2) for the single shared registry
    both transports dispatch from (§V14); its ``input_schema`` is the bounded
    model's JSON Schema, so the §V19 ``page_size`` bound + §V18 caps land on the
    wire exactly as validated.
    """

    def handler(**params: object) -> ResponseEnvelope:
        # §V18/§V19 gate: the bounded model rejects an out-of-range page_size, an
        # over-length id, both/neither selector, or an unknown parameter *before*
        # any query runs -- a ValidationError propagates as a protocol-level
        # rejection, never a silently widened page (§V19).
        parsed = GetStageInput.model_validate(params)
        return run_guarded(
            get_conn,
            lambda conn: get_stage(
                conn,
                server=parsed.server,
                stage_code=parsed.stage_code,
                game_id=parsed.game_id,
                include_map=parsed.include_map,
                include_routes=parsed.include_routes,
                include_spawns=parsed.include_spawns,
                map_page=parsed.map_page.page,
                map_page_size=parsed.map_page.page_size,
                routes_page=parsed.routes_page.page,
                routes_page_size=parsed.routes_page.page_size,
                spawns_page=parsed.spawns_page.page,
                spawns_page_size=parsed.spawns_page.page_size,
            ),
            _shape,
        )

    return ToolSpec(
        name=_TOOL_NAME,
        title=_TOOL_TITLE,
        description=_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(GetStageInput),
    )
