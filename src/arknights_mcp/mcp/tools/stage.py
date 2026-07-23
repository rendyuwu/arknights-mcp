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

from arknights_mcp.mcp.envelopes import Provenance, ResponseEnvelope, error, ok
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._shared import (
    LIST_FIELD_CONVENTION,
    ConnectionProvider,
    absent_field_limitation,
    observation_to_dict,
    page_to_dict,
    run_guarded,
)
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.stages import AnalysisDepth, AnalyzeStageInput, GetStageInput
from arknights_mcp.services.stage_map_render import RenderedMap
from arknights_mcp.services.stage_tile_grid import TileGridFacts
from arknights_mcp.services.stages import (
    EnemyOccurrenceFacts,
    RouteFacts,
    SpawnFacts,
    StageAnalysisResult,
    StageDetailResult,
    StageFacts,
    StageMapFacts,
    analyze_stage,
    get_stage,
)

_TOOL_NAME = "get_stage"
_TOOL_TITLE = "Get stage"
_TOOL_DESCRIPTION = (
    "Fetch one Arknights stage's facts by region + stage_code (e.g. 4-4) or "
    "game_id. The default response is compact facts + provenance; set include_map "
    "/ include_routes / include_spawns to add the tile grid, enemy routes, or spawn "
    "timeline. The tile grid comes as tile_grid: one string per grid row (top row "
    "first) plus a legend mapping each character to its tile fields; absent_symbol "
    "marks a cell with no tile. A tile's tile_key/buildable_type describe where you "
    "may DEPLOY (a tile_forbidden tile blocks deployment), while passable describes "
    "whether ENEMIES may cross it -- so a forbidden tile can still be passable; the "
    "two are not in conflict. Enemy routes are collapsed to distinct geometry: each "
    "entry carries an occurrence_count and the raw route_indices that share it. "
    "Spawn timeline values (spawn_time and interval) are in seconds. Set "
    "include_map_image for a rendered SVG map drawn from the stage's own grid data "
    "(a derived image, not game artwork); a very large map is omitted with a note. "
    "en/cn are never mixed. " + LIST_FIELD_CONVENTION
)

#: §V74 (d): the standing gloss attached to every response that emits ``tile_grid``.
#: The raw source pairs a ``tile_forbidden`` tile_key with ``passable:true``, which
#: reads as a contradiction; it is not. Client-facing prose only -- no spec cites or
#: internal jargon (§V71).
_TILE_GRID_LIMITATION = (
    "tile_key and buildable_type describe deployment (where you can place "
    "operators); passable describes enemy movement. A tile_forbidden or "
    "non-buildable tile can still be passable by enemies -- the two are separate "
    "properties, not a contradiction."
)

_NOT_FOUND_MESSAGE = "no stage matched the given region and selector"
_NOT_FOUND_ACTION = (
    "verify the server and stage_code/game_id (use search_stages to find the stage), "
    "or ask the server admin to run `arknights-mcp status` to check the active build"
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


def _tile_grid_to_dict(grid: TileGridFacts) -> dict[str, object]:
    # §V74 (c): the compact per-row grid -- one string per row (top row first) +
    # a legend decoding each character. A whole board rides one response instead of
    # the ~3 pages the per-tile object dump needed.
    return {
        "rows": list(grid.rows),
        "absent_symbol": grid.absent_symbol,
        "legend": [
            {
                "symbol": entry.symbol,
                "tile_key": entry.tile_key,
                "height_type": entry.height_type,
                "buildable_type": entry.buildable_type,
                "passable": entry.passable,
            }
            for entry in grid.legend
        ],
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
    # §V74 (a): one row per DISTINCT geometry, not per raw record. ``route_indices``
    # are the raw indices that share this geometry (a spawn's ``route_index`` joins
    # to one of them); ``occurrence_count`` is how many records collapsed here.
    return {
        "route_indices": list(route.route_indices),
        "occurrence_count": route.occurrence_count,
        "start_position": route.start_position,
        "end_position": route.end_position,
        "checkpoints": route.checkpoints,
    }


def _spawn_to_dict(spawn: SpawnFacts) -> dict[str, object]:
    out: dict[str, object] = {
        "wave_index": spawn.wave_index,
        "enemy_game_id": spawn.enemy_game_id,
        "enemy_level_variant": spawn.enemy_level_variant,
        "variant_id": spawn.variant_id,
        "route_index": spawn.route_index,
        "spawn_time": spawn.spawn_time,
        "count": spawn.count,
        "interval": spawn.interval,
        "hidden": spawn.hidden,
    }
    # §V67: ``spawn_group`` is an always-optional scalar -- omit the key when the
    # source carried none rather than emit an ambiguous null (additive-safe, §V21).
    if spawn.spawn_group is not None:
        out["spawn_group"] = spawn.spawn_group
    return out


#: §V67/B58 expected stage scalars a client reasonably looks for; when the source omits
#: one, it is named in a "not present in source" limitation (§V26).
def _stage_absent_field_limitations(stage: StageFacts) -> tuple[str, ...]:
    """§V67/§V26 (B58): name the expected stage scalars absent from the source.

    ``recommended_level`` / ``max_life_points`` are surfaced as a "not present in
    source" limitation when the source omitted them, so an absent value is called out
    rather than left as an ambiguous null. Returns the single standing limitation
    naming them (empty when both are present)."""
    absent: list[str] = []
    if stage.recommended_level is None:
        absent.append("recommended_level")
    if stage.max_life_points is None:
        absent.append("max_life_points")
    return absent_field_limitation(absent)


def _map_image_to_dict(image: RenderedMap) -> dict[str, object]:
    """The render-own map image for the wire (§T122).

    ``content`` is the inline SVG document (an image content payload, not a URL
    reference -- §V63 is a different path); ``media_type`` is ``image/svg+xml``.
    The document is a DERIVED render from the stage's own typed grid data -- it
    embeds no third-party art byte (§V16)."""
    return {
        "format": "svg",
        "media_type": image.media_type,
        "content": image.svg,
        "pixel_width": image.pixel_width,
        "pixel_height": image.pixel_height,
        "tile_count": image.tile_count,
        # §T140 (B65): a colour legend so a client can decode the opaque tile fills and
        # route markers the derived SVG carries -- the render shipped none before.
        "legend": [dict(entry) for entry in image.legend],
    }


def _shape(result: StageDetailResult) -> ResponseEnvelope:
    """Map the domain result to a typed §V23 envelope (§V5 region + provenance)."""
    if result.status == "not_found" or result.stage is None:
        return error("not_found", _NOT_FOUND_MESSAGE, suggested_action=_NOT_FOUND_ACTION)

    data: dict[str, object] = {"stage": _stage_to_dict(result.stage)}
    tile_grid_limitation: tuple[str, ...] = ()
    if result.stage_map is not None:
        data["map"] = _map_header_to_dict(result.stage_map)
        if result.tile_grid is not None:
            data["tile_grid"] = _tile_grid_to_dict(result.tile_grid)
            # §V74 (d): the forbidden-vs-passable gloss rides every grid response.
            tile_grid_limitation = (_TILE_GRID_LIMITATION,)
    if result.routes_page is not None:
        data["routes"] = [_route_to_dict(r) for r in result.routes]
        data["routes_page"] = page_to_dict(result.routes_page)
    if result.spawns_page is not None:
        data["spawns"] = [_spawn_to_dict(s) for s in result.spawns]
        data["spawns_page"] = page_to_dict(result.spawns_page)
    if result.map_image is not None:
        data["map_image"] = _map_image_to_dict(result.map_image)

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
        # §V74 (d) forbidden-vs-passable gloss (when a grid is emitted), the §V22 map
        # caption (if any), plus the §V67/§V26 (B58) "not present in source" limitation
        # naming any expected stage scalar the source omitted.
        limitations=(
            *tile_grid_limitation,
            *result.limitations,
            *_stage_absent_field_limitations(result.stage),
        ),
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
                include_map_image=parsed.include_map_image,
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
    "roster + analyzer warnings), detailed (+ full per-enemy stat and timing "
    "context, with attack_interval and spawn times in seconds). en/cn are never "
    "mixed."
)


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
    """The full typed occurrence for ``depth=detailed``: identity + class/motion/attack
    + timing PLUS the §V47 per-enemy stat block (hp/atk/def/res/attack_interval/
    move_speed/weight). The stat block reads variant stats over base (§V46 COALESCE),
    so the description's "full per-enemy stat/timing context" is honoured, not just
    advertised (B41). A stat is ``null`` when the source field is absent (§V26)."""
    return {
        "game_id": occ.game_id,
        "display_name": occ.display_name,
        "enemy_class": occ.enemy_class,
        "is_boss": occ.is_boss,
        "is_elite": occ.is_elite,
        "motion_type": occ.motion_type,
        "attack_type": occ.attack_type,
        "level_variant": occ.level_variant,
        "variant_id": occ.variant_id,
        "total_count": occ.total_count,
        "hp": occ.hp,
        "atk": occ.atk,
        "def": occ.def_,
        "res": occ.res,
        "attack_interval": occ.attack_interval,
        "move_speed": occ.move_speed,
        "weight": occ.weight,
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
        "observations": [observation_to_dict(o) for o in result.observations],
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
