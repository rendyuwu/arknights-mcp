"""``get_stage`` + ``analyze_stage`` MCP tools (§T34/§T40; §V6/§V19/§V22/§V23;
§I.tool).

Both bridge a bounded input model (§T30) to a shared
:mod:`~arknights_mcp.services.stages` service (§V14) and wrap the outcome in the
typed :class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope` (§T29). A tool owns
no query logic -- only the model -> service -> envelope mapping -- so both
transports dispatch identical read-only (§V2) behaviour from the single registry.
They share this module (and its ``_stage_to_dict`` shaper, §V37) because both act
on one stage.

``get_stage`` returns facts (+ opt-in map/routes/spawns); ``analyze_stage`` (§T40)
returns the deterministic threat **observations**, each carrying every §V6 field
(``rule_id`` + evidence + confidence + limitations + ``analyzer_version``), scaled
by a ``depth`` lever (summary / standard / detailed). It emits facts + evidence-
backed observations only -- never a mandatory or best-in-slot recommendation (§V7).

Two invariants are load-bearing for ``get_stage``:

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

from arknights_mcp.analyzers import EvidenceItem, Observation
from arknights_mcp.mcp.envelopes import Provenance, ResponseEnvelope, error, ok
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._shared import ConnectionProvider, run_guarded
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.stages import AnalysisDepth, AnalyzeStageInput, GetStageInput
from arknights_mcp.services.stages import (
    EnemyOccurrenceFacts,
    RouteFacts,
    SectionPage,
    SpawnFacts,
    StageAnalysisResult,
    StageDetailResult,
    StageFacts,
    StageMapFacts,
    TileFacts,
    analyze_stage,
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


# --- analyze_stage (§T40): deterministic threat observations, depth-scaled. ----

_ANALYZE_TOOL_NAME = "analyze_stage"
_ANALYZE_TOOL_TITLE = "Analyze stage"
_ANALYZE_TOOL_DESCRIPTION = (
    "Analyze one Arknights stage (by region + stage_code, e.g. 4-4, or game_id) "
    "into deterministic, evidence-backed threat observations: each carries a "
    "rule_id, typed evidence, a confidence score, and limitations -- facts and "
    "observations only, never a mandatory or best-in-slot recommendation. depth "
    "scales the surrounding facts: summary (observations only), standard (+ enemy "
    "roster + analyzer warnings), detailed (+ full per-enemy stat/timing context). "
    "en/cn are never mixed."
)


def _evidence_to_dict(item: EvidenceItem) -> dict[str, object]:
    """One typed datum that drove an observation (§V6 evidence)."""
    return {"ref": item.ref, "field": item.field, "value": item.value, "note": item.note}


def _observation_to_dict(obs: Observation) -> dict[str, object]:
    """One evidence-backed observation with every §V6 field intact.

    Emitted in full at *every* depth: a surfaced inference always carries its
    ``rule_id`` + evidence + confidence + limitations + ``analyzer_version``, never
    a bare verdict (§V6). ``depth`` scales the surrounding facts, not the evidence.
    """
    return {
        "rule_id": obs.rule_id,
        "category": obs.category,
        "tag": obs.tag,
        "title": obs.title,
        "summary": obs.summary,
        "confidence": obs.confidence,
        "evidence": [_evidence_to_dict(e) for e in obs.evidence],
        "limitations": list(obs.limitations),
        "analyzer_version": obs.analyzer_version,
    }


def _occurrence_compact(occ: EnemyOccurrenceFacts) -> dict[str, object]:
    """The enemy-roster row for ``depth=standard``: identity + how many, no stats."""
    return {
        "game_id": occ.game_id,
        "display_name": occ.display_name,
        "is_boss": occ.is_boss,
        "is_elite": occ.is_elite,
        "total_count": occ.total_count,
    }


def _occurrence_full(occ: EnemyOccurrenceFacts) -> dict[str, object]:
    """The full typed occurrence for ``depth=detailed`` (class/motion/attack + timing)."""
    return {
        "game_id": occ.game_id,
        "display_name": occ.display_name,
        "enemy_class": occ.enemy_class,
        "is_boss": occ.is_boss,
        "is_elite": occ.is_elite,
        "motion_type": occ.motion_type,
        "attack_type": occ.attack_type,
        "level_variant": occ.level_variant,
        "total_count": occ.total_count,
        "first_spawn_time": occ.first_spawn_time,
        "last_spawn_time": occ.last_spawn_time,
        "route_count": occ.route_count,
    }


def _shape_analysis(depth: AnalysisDepth, result: StageAnalysisResult) -> ResponseEnvelope:
    """Map the analysis result to a typed §V23 envelope, scaled by ``depth``.

    Every depth emits full §V6 observations (evidence never dropped) + region +
    provenance (§V5) + the analyzer version. The ``depth`` lever only widens the
    surrounding *facts*: ``summary`` is observations-only; ``standard`` adds the
    compact enemy roster + the analyzer's §V26 conflict warnings; ``detailed`` swaps
    in the full per-enemy typed context. The §V22 cap still fails closed on any
    oversized payload.
    """
    if result.status == "not_found" or result.stage is None:
        return error("not_found", _NOT_FOUND_MESSAGE, suggested_action=_NOT_FOUND_ACTION)

    data: dict[str, object] = {
        "depth": depth,
        "stage": _stage_to_dict(result.stage),
        "observations": [_observation_to_dict(o) for o in result.observations],
    }
    if depth != "summary":
        shaper = _occurrence_full if depth == "detailed" else _occurrence_compact
        data["occurrences"] = [shaper(o) for o in result.occurrences]
        data["warnings"] = list(result.warnings)

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
        analyzer_version=result.analyzer_version,
    )


def build_analyze_stage_spec(get_conn: ConnectionProvider) -> ToolSpec:
    """Build the ``analyze_stage`` :class:`ToolSpec` (§T40; §V6/§V7/§V14).

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build. The spec is read-only (§V2) for the single shared registry both
    transports dispatch from (§V14); its ``input_schema`` is the bounded model's
    JSON Schema, so the §V5 required ``server``, the exactly-one selector, and the
    ``depth`` enum land on the wire exactly as validated.
    """

    def handler(**params: object) -> ResponseEnvelope:
        # §V5/§V18 gate: the bounded model requires a region + exactly one selector,
        # constrains ``depth`` to the enum, and rejects an unknown parameter *before*
        # any query runs -- a ValidationError propagates as a protocol-level rejection.
        parsed = AnalyzeStageInput.model_validate(params)
        return run_guarded(
            get_conn,
            lambda conn: analyze_stage(
                conn,
                server=parsed.server,
                stage_code=parsed.stage_code,
                game_id=parsed.game_id,
            ),
            lambda result: _shape_analysis(parsed.depth, result),
        )

    return ToolSpec(
        name=_ANALYZE_TOOL_NAME,
        title=_ANALYZE_TOOL_TITLE,
        description=_ANALYZE_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(AnalyzeStageInput),
    )
