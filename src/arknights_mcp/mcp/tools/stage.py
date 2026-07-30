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

from dataclasses import asdict

from arknights_mcp.mcp.envelopes import Provenance, ResponseEnvelope, error, ok
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._enum_legend import (
    TOOL_ENUM_LEGEND_FIELDS,
    attach_enum_legend,
)
from arknights_mcp.mcp.tools._shared import (
    CONFIDENCE_SCALE_NOTE,
    ENEMY_CLASS_NOTE,
    ENEMY_STAT_SCALE_NOTE,
    LEVEL_VARIANT_NOTE,
    LIST_FIELD_CONVENTION,
    RETIRED_ATTACK_TYPE_NOTE,
    STAGE_MAP_GUIDE_POINTER,
    ConnectionProvider,
    absent_field_limitation,
    observation_to_dict,
    page_to_dict,
    run_guarded,
)
from arknights_mcp.mcp.tools._stage_selector import (
    STAGE_SELECTOR_NOTE,
    stage_ambiguity_limitation,
)
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.stages import AnalysisDepth, AnalyzeStageInput, GetStageInput
from arknights_mcp.services.stage_map_render import RenderedMap
from arknights_mcp.services.stage_route_digest import RouteFacts
from arknights_mcp.services.stage_tile_grid import TileGridFacts
from arknights_mcp.services.stages import (
    EnemyOccurrenceFacts,
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

#: §V79/§V108 (B153): ``get_stage`` and ``analyze_stage`` are COMPLEMENTARY siblings --
#: one domain, split payload -- and §V79 covered only OVERLAPPING ones until B153, so
#: neither description had ever mentioned the other. The cost was measured live: a client
#: that entered through ``analyze_stage`` was told the route geometry was "not clustered",
#: had no way to learn ``get_stage`` holds it, concluded the server did not have it, and
#: offered a community wiki instead. §V79 requires the cross-ref to be BIDIRECTIONAL and
#: to name the FLAG, not just the sibling tool: "see get_stage" would leave the caller to
#: guess which of four include_ flags carries routes. Client-facing text, so no internal
#: cites or jargon (§V71 b); short sentences (§V71 f).
ANALYZE_SIBLING_NOTE = (
    "For threat observations on this stage -- aerial enemies, damage-type skews, spawn "
    "pressure, approach routes -- call analyze_stage. It reports on this data rather "
    "than returning it."
)
STAGE_FACTS_SIBLING_NOTE = (
    "This tool returns observations, not the underlying map data. For the stage's tile "
    "grid, clustered route geometry, or per-wave spawn rows, call get_stage with "
    "include_map, include_routes, or include_spawns."
)
#: §V71 (f)/§V111 (§T207, B156). At 2881 chars this was the LONGEST description on the
#: server -- the crown B145's ``get_operator`` halving moved rather than removed, and the
#: same truncation exposure (a client tool-listing cutting the string mid-sentence, which
#: costs the caller the pre-call facts that lead it). Nothing was deleted to bring it under
#: the §V71 (f) budget (§V111 b forbids that); three blocks MOVED to named homes:
#:
#: * the ~970-char tile-grid/route/checkpoint/spawn reading guide -> the MCP resource
#:   :data:`STAGE_MAP_GUIDE_URI`, named here by a one-line pointer (§V84/B144 home);
#: * the ``difficulty`` + ``stage_type`` value domains -> the response's ``enum_legend``
#:   (§V104 b), which arrives beside the values it decodes;
#: * the deploy-vs-passable gloss -> nowhere, because it was already DUPLICATED by
#:   :data:`_TILE_GRID_LIMITATION`, which rides every grid-bearing response (§V37).
#:
#: §T195/§V111 (b) added the §V102 selector contract (:data:`STAGE_SELECTOR_NOTE`) and paid
#: for it by MOVING a fourth block rather than deleting one: :data:`LEVEL_VARIANT_NOTE` now
#: rides as a limitation on the responses that actually emit the key it decodes -- the
#: spawn rows' ``enemy_level_variant`` (this tool emits ``level_variant`` nowhere else), so
#: it reaches the client beside the value instead of billing every caller who never sets
#: ``include_spawns``. Same call §T207 made for the stat-scale + confidence notes.
#:
#: What leads is what a caller needs BEFORE the call (§V111 c): the selector, a worked
#: example, and what each include_ flag adds.
_TOOL_DESCRIPTION = (
    "Fetch one Arknights stage's facts by region + stage_code (e.g. 4-4) or "
    "game_id. " + STAGE_SELECTOR_NOTE + " The default response is compact facts + "
    "provenance; set include_map "
    "/ include_routes / include_spawns to add the tile grid, enemy routes, or spawn "
    "timeline. Set include_map_image for a rendered SVG map drawn from the stage's own "
    "grid data (a derived image, not game artwork); a very large map is omitted with a "
    "note. The SVG is for display only -- do not reason from the image; for tile-level "
    "reasoning use include_map's tile_grid. " + STAGE_MAP_GUIDE_POINTER + " "
    "Spawn timeline values (spawn_time and interval) are in seconds. "
    "difficulty is the stage variant tag; the response's enum_legend gives its values "
    "and those of stage_type. "
    + ANALYZE_SIBLING_NOTE
    + " en/cn are never mixed. "
    + LIST_FIELD_CONVENTION
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
    """The compact, always-present stage facts (no prose; §V16/§V18).

    §V67 (B98): ``recommended_level`` / ``max_life_points`` are omitted when the
    source carried none, never emitted as null -- the standing absent-field
    limitation is the sole absence signal.

    §V69 (T186): ``zone_display_name`` pairs the opaque ``zone_game_id`` with a readable
    zone name, so ``zone_game_id: "act12d0"`` stops being an id the client must
    either abandon or invent a meaning for. Omitted when the zone is unnamed in source
    (§V67) -- the id still ships, so the absence is visible rather than fabricated.

    §V110 (B155): ``event_name`` names the EVENT that zone belongs to ("Lone Trail"),
    which the zone name does not -- ``zone_display_name`` is the sub-zone subtitle
    ("The Coming of The Future"), so a client given only that cannot say which event a
    stage is from. Omitted for a zone with no event (§V67)."""
    out: dict[str, object] = {
        "server": stage.server,
        "game_id": stage.game_id,
        "stage_code": stage.stage_code,
        "display_name": stage.display_name,
        "zone_game_id": stage.zone_game_id,
        "stage_type": stage.stage_type,
        "difficulty": stage.difficulty,
        "sanity_cost": stage.sanity_cost,
    }
    if stage.zone_display_name is not None:
        out["zone_display_name"] = stage.zone_display_name
    if stage.event_name is not None:
        out["event_name"] = stage.event_name
    if stage.recommended_level is not None:
        out["recommended_level"] = stage.recommended_level
    if stage.max_life_points is not None:
        out["max_life_points"] = stage.max_life_points
    return out


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
        "route_index": spawn.route_index,
        "spawn_time": spawn.spawn_time,
        "count": spawn.count,
        "interval": spawn.interval,
        "hidden": spawn.hidden,
    }
    # §V67/B90: ``variant_id`` (inline ``useDb:false`` variant, §T80) and
    # ``spawn_group`` are always-optional scalars -- omit the key when the source
    # carried none rather than emit an ambiguous null (additive-safe, §V21).
    if spawn.variant_id is not None:
        out["variant_id"] = spawn.variant_id
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
    # §V104 (b): the STATIC domains of the two enums every stage row carries, hoisted
    # beside the values instead of spelled out in the description (§V111 contention).
    enum_limitations = attach_enum_legend(data, TOOL_ENUM_LEGEND_FIELDS[_TOOL_NAME], ())
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
    spawn_limitation: tuple[str, ...] = ()
    if result.spawns_page is not None:
        data["spawns"] = [_spawn_to_dict(s) for s in result.spawns]
        data["spawns_page"] = page_to_dict(result.spawns_page)
        # §V104/§V111 (a) (§T195): the join-key gloss rides the section that emits the
        # key (spawn rows' ``enemy_level_variant``), not every caller's description.
        spawn_limitation = (LEVEL_VARIANT_NOTE,)
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
        # §V102 (b)/B139: when the requested stage_code named more than one stage, say
        # WHICH one answered and name the alternates -- the pick was silent before, so a
        # client could not tell it had been given half the answer.
        limitations=(
            *stage_ambiguity_limitation(result.ambiguity),
            *tile_grid_limitation,
            *spawn_limitation,
            *result.limitations,
            *_stage_absent_field_limitations(result.stage),
            *enum_limitations,
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
#: §V79/§V111 (b): this description sat at 1590 of the 1600-char §V71 (f) budget, so the
#: §V108/B153 cross-ref (:data:`STAGE_FACTS_SIBLING_NOTE`) had to be PAID for, not merely
#: appended -- and §V111 (b) forbids paying by deleting a §V-mandated fact. So
#: :data:`LEVEL_VARIANT_NOTE` MOVED to a ``depth="detailed"`` limitation, beside the
#: enum_legend and stat-scale notes already gated there. The gate is exact: only the
#: detailed occurrence row emits ``level_variant``, so at summary/standard depth the note
#: decoded a key the response did not carry and billed every caller for it. Same call
#: §T195 made on ``get_stage`` (where it moved to the spawns-gated section) and §T207 made
#: for the stat-scale and confidence notes.
_ANALYZE_TOOL_DESCRIPTION = (
    "Analyze one Arknights stage (by region + stage_code, e.g. 4-4, or game_id) "
    "into deterministic, evidence-backed threat observations. " + STAGE_SELECTOR_NOTE + " "
    "Each observation carries a "
    "rule_id, typed evidence, a confidence score, and limitations -- facts and "
    "observations only, never a mandatory or best-in-slot recommendation. depth "
    "scales the surrounding facts: summary (observations only), standard (+ enemy "
    "roster + analyzer warnings), detailed (+ full per-enemy stat and timing "
    "context, with attack_interval and spawn times in seconds). "
    + STAGE_FACTS_SIBLING_NOTE
    + " "
    + ENEMY_CLASS_NOTE
    + " The response's enum_legend gives the values of enemy_class. "
    "en/cn are never mixed. " + LIST_FIELD_CONVENTION
)

#: §V67/§V26 (B98 follow-through): the detailed occurrence rows omit an
#: absent-in-source ``damage_types`` key, so the envelope must still carry the
#: absence signal (limitation = sole signal, mirroring ``get_enemy``). Client-facing
#: text, so no internal cites/jargon (§V71) -- the cites live in this comment.
#:
#: §V113/B160: the RETIRED ``attack_type`` gets its own shared sentence
#: (:data:`RETIRED_ATTACK_TYPE_NOTE`) instead, because its absence is corpus-wide and
#: this per-stage phrasing would understate it (§V108).
_OCCURRENCE_DAMAGE_TYPES_LIMITATION = (
    "damage_types is not present in the source data for one or more enemies in this "
    "stage; those occurrence rows omit the key."
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
    advertised (B41). A stat is ``null`` when the source field is absent (§V26).
    ``variant_id`` (the inline ``useDb:false`` variant id, §T80) is OMITTED rather
    than emitted as a bare null for a base-enemy occurrence (§V67/B90); the
    ``attack_type`` scalar is likewise omitted when absent in source (§V67/B98).

    §V101/§T197: ``damage_types`` / ``attack_range`` / ``targeting`` ride here too -- the
    ranged-arts rule cites all three as evidence field paths, and a path this response
    does not carry is one a client cannot look up. Same §V67 discipline: an absent scalar
    omits its key, and ``damage_types`` is ``[]`` when the source confirms none, omitted
    when the source carried no such field (B58). An evidence row for one of these fields
    exists only when the rule read a value, so the row and its key appear together.

    ``block_behavior`` / ``abilities`` are NOT here: the rules that cited them were
    retired in §T210 (c) because no source fills either column, so their §V101
    justification went with them. ``immunities`` stays a ``get_enemy`` fact -- it is
    per-enemy detail no stage rule decides from, and this row is already the widest one
    the §V22 cap has to hold."""
    out: dict[str, object] = {
        "game_id": occ.game_id,
        "display_name": occ.display_name,
        "enemy_class": occ.enemy_class,
        "is_boss": occ.is_boss,
        "is_elite": occ.is_elite,
        "motion_type": occ.motion_type,
        "level_variant": occ.level_variant,
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
    # §V67/B90: inline-variant id present only for a ``useDb:false`` variant row;
    # a base-enemy occurrence omits the key rather than carrying an ambiguous null.
    if occ.variant_id is not None:
        out["variant_id"] = occ.variant_id
    # §V67/B98: absent-in-source scalar omitted, never null.
    if occ.attack_type is not None:
        out["attack_type"] = occ.attack_type
    if occ.attack_range is not None:
        out["attack_range"] = occ.attack_range
    # §V114/B161: an absent attack_range that the source ANSWERED ("no attack radius")
    # rather than left unstated. Emitted only where it answered -- a false flag says
    # nothing -- and it rides here because the rule reads it: when it conflicts with
    # targeting the analysis warns instead of concluding, and the warning names a field
    # this response must therefore carry.
    if occ.attack_range_declared_none:
        out["attack_range_declared_none"] = True
    if occ.targeting is not None:
        out["targeting"] = occ.targeting
    # §V67/B58: [] = the source confirms none; key absent = the source carried no field.
    if occ.damage_types is not None:
        out["damage_types"] = list(occ.damage_types)
    return out


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

    # §V101/§T197: the stage-level metrics the lane/route + tiles/deploy observations cite
    # as evidence field paths (``metrics.route_record_count``, ``metrics.tile_total``, ...)
    # ride the stage block, so every evidence path resolves against the record its ``ref``
    # names. Additive (§V21); omitted whole when no metric was loaded and per key when a
    # single datum is absent (§V67 -- absent, never a null standing for zero).
    stage_block = _stage_to_dict(result.stage)
    if result.metrics is not None:
        stage_block["metrics"] = {
            key: value for key, value in asdict(result.metrics).items() if value is not None
        }
    data: dict[str, object] = {
        "depth": depth,
        "stage": stage_block,
        "observations": [observation_to_dict(o) for o in result.observations],
    }
    if depth != "summary":
        shaper = _occurrence_full if depth == "detailed" else _occurrence_compact
        data["occurrences"] = [shaper(o) for o in result.occurrences]
        data["warnings"] = list(result.warnings)

    # §V67/B98: the shared stage shaper omits absent scalars (recommended_level /
    # max_life_points), so this surface carries the same sole-signal limitation
    # naming them as get_stage does; a detailed occurrence row likewise omits an
    # absent damage_types, so that omission is named too (sole signal, never silent).
    # §V102 (b)/B139: the shared-stage_code disclosure leads, at every depth -- which
    # stage was analyzed is the first thing a client must be able to check.
    limitations = (
        *stage_ambiguity_limitation(result.ambiguity),
        *_stage_absent_field_limitations(result.stage),
    )
    if depth == "detailed":
        if any(o.damage_types is None for o in result.occurrences):
            limitations = (*limitations, _OCCURRENCE_DAMAGE_TYPES_LIMITATION)
        # §V113/B160: the retired scalar's absence is a fact about the game data, not
        # about this stage, so it is stated once with its true scope (§V108).
        if any(o.attack_type is None for o in result.occurrences):
            limitations = (*limitations, RETIRED_ATTACK_TYPE_NOTE)
    # §V104 (b): only the DETAILED occurrence row carries enemy_class, so only it needs
    # the domain -- a legend for a field this response omits would be noise (§V67).
    # §V104/§V71 (e): the same gate carries the stat scales -- only the detailed row
    # emits res/move_speed/weight, and they are undecidable as bare numbers.
    if depth == "detailed":
        limitations = attach_enum_legend(
            data, TOOL_ENUM_LEGEND_FIELDS[_ANALYZE_TOOL_NAME], limitations
        )
        # §V111 (b)/§T203: the level_variant join gloss, MOVED off the description (which
        # had 10 chars of §V71 (f) headroom left) onto the one depth that emits the key it
        # decodes. Never deleted -- §V111 (b) forbids buying budget with a mandated fact.
        limitations = (*limitations, ENEMY_STAT_SCALE_NOTE, LEVEL_VARIANT_NOTE)
    # §V104/§V6: the confidence scale rides the response that carries a confidence,
    # stated ONCE per envelope (§V66) rather than in the description (§V111 a).
    if result.observations:
        limitations = (*limitations, CONFIDENCE_SCALE_NOTE)

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
        limitations=limitations,
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
