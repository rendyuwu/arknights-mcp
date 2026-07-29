"""Internal stage analysis service (§T17): the single domain entry point both
transports call to analyze a stage (§V14).

Given a read-only SQLite connection and a ``(server, stage)`` selector, it loads
the stage facts + region + provenance (§V5) and the stage's typed enemy
occurrences, builds a :class:`~arknights_mcp.analyzers.base.StageThreatContext`,
and runs the deterministic threat analyzer. Every observation it returns keeps
the five §V6 fields the analyzer stamped (``rule_id`` + evidence + confidence +
limitations + ``analyzer_version``); the service adds no natural-language
interpretation of its own.

Read-only + parameterized SQL only (§V2): the parameterized ``SELECT``s live in
:class:`~arknights_mcp.db.repositories.stages.StageRepository` (§T20), the sole
sanctioned SQL surface; this service only reads through it and never mutates the
database. It does not open the connection (the read-only connection factory is
:func:`~arknights_mcp.db.connection.open_read_only`); callers pass one in, so
both transports share this exact function (§V14). No transport-specific logic
lives here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from arknights_mcp.analyzers import (
    EnemyOccurrence,
    Observation,
    StageThreatContext,
    StageTiles,
)
from arknights_mcp.analyzers import (
    analyze_stage as run_threat_analysis,
)
from arknights_mcp.db.repositories.stages import (
    StageMapRow,
    StageRepository,
    StageRouteRow,
    StageRow,
)
from arknights_mcp.models.common import PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX
from arknights_mcp.services.stage_map_render import (
    MAX_MAP_CELLS,
    MAX_MAP_ROUTES,
    MapCell,
    MapRoute,
    RenderedMap,
    render_stage_map,
)
from arknights_mcp.services.stage_route_digest import (
    RouteFacts,
    _checkpoint_points,
    _distinct_routes,
    _point_xy,
    _route_truncated_limitation,
    unknown_checkpoint_type_limitation,
)
from arknights_mcp.services.stage_tile_grid import (
    TileGridFacts,
    resolve_tile_grid,
)
from arknights_mcp.services.stage_variant import stage_variant
from arknights_mcp.util.coerce import json_load

#: Typed outcome of a stage lookup. The full §V23 status vocabulary is wired
#: into the tool envelope in §T29; the M0 service reports only these two.
StageAnalysisStatus = Literal["ok", "not_found"]


@dataclass(frozen=True)
class StageProvenance:
    """Region-scoped provenance for a factual stage response (§V5)."""

    snapshot_id: str
    imported_at: str


@dataclass(frozen=True)
class StageFacts:
    """Typed, allowlisted facts about one stage (no prose; §V16, §V18)."""

    server: str
    game_id: str
    stage_code: str | None
    display_name: str | None
    zone_game_id: str | None
    #: §V69/T186: the readable name for ``zone_game_id``. A bare opaque id forces a
    #: second lookup the client has no tool for, or invites it to guess; ``None`` when
    #: the zone carries no name in source, and the tool layer omits the key there.
    zone_display_name: str | None
    #: §V110/B155: the TITLE of the event the zone belongs to ("Lone Trail"), which is
    #: a different string from a different source file than the sub-zone subtitle in
    #: ``zone_display_name``. ``None`` when the zone belongs to no event.
    event_name: str | None
    stage_type: str | None
    difficulty: str | None
    sanity_cost: int | None
    recommended_level: int | None
    max_life_points: int | None
    provenance: StageProvenance


@dataclass(frozen=True)
class EnemyOccurrenceFacts:
    """One enemy's typed appearance in the stage (from ``stage_enemies``).

    Carries the §V47 per-enemy stat block (``hp`` / ``atk`` / ``def_`` / ``res`` /
    ``attack_interval`` / ``move_speed`` / ``weight``) the ``analyze_stage``
    ``depth=detailed`` occurrence promises; each is ``None`` when the level row or
    the source field is absent (§V26).

    ``variant_id`` is set for a stage-scoped inline variant (§T80/§V43), whose
    ``motion_type`` and stat block already read the variant's value over the base
    prefab (COALESCE in the repository; §V46); ``None`` for a plain base-enemy
    occurrence.

    ``damage_types`` / ``attack_range`` / ``targeting`` are the typed fields the
    ranged-arts rule decides from. They are carried here (§T197/§V101) because that rule
    cites them as evidence field paths, and a path this response does not emit is one a
    client cannot look up -- the same defect as the packed ``"def/res"`` pseudo-field
    (B137), and the same over-promising the ``detailed`` depth was already caught doing
    in B41. ``damage_types is None`` means the source carried no such field; ``()`` means
    present-but-empty (§V26/B58).

    ``block_behavior`` / ``abilities`` are gone (§T210 (c)): no real source fills either
    column, so the three rules that decided from them were retired rather than left
    registered and unable to fire (B160 (c)) -- and a field no rule reads and no build
    populates is not an occurrence fact, it is a NULL with a name.
    """

    game_id: str
    display_name: str | None
    enemy_class: str | None
    is_boss: bool
    is_elite: bool
    motion_type: str | None
    #: The RETIRED handbook scalar (§V113): NULL on every real enemy. ``damage_types``
    #: carries the damage kind now (§T210/B160).
    attack_type: str | None
    damage_types: tuple[str, ...] | None
    level_variant: int
    total_count: int | None
    first_spawn_time: float | None
    last_spawn_time: float | None
    route_count: int | None
    hp: int | None
    atk: int | None
    def_: int | None
    res: int | None
    attack_interval: float | None
    move_speed: float | None
    weight: int | None
    variant_id: str | None
    attack_range: float | None = None
    targeting: str | None = None


@dataclass(frozen=True)
class StageMetrics:
    """The stage-level scalars the threat rules decide from (§V101 evidence targets).

    ``route_record_count`` is deliberately not called ``route_count``: it counts raw
    enemy-route RECORDS, which share start/end/checkpoint geometry, so it is not a
    distinct-lane tally (§V49/B43) -- and the enemy occurrences already carry a
    ``route_count`` meaning something narrower (how many records THAT enemy splits
    across), so one name for both would be two meanings under one key (§V99).
    Every field is optional: absent means the datum was not loaded, never zero (§V26/§V67).
    """

    route_record_count: int | None = None
    tile_total: int | None = None
    buildable_melee: int | None = None
    buildable_ranged: int | None = None

    def is_empty(self) -> bool:
        """True when no metric was loaded, so the block is omitted rather than emitted
        as an all-null object (§V67)."""
        return all(
            v is None
            for v in (
                self.route_record_count,
                self.tile_total,
                self.buildable_melee,
                self.buildable_ranged,
            )
        )


@dataclass(frozen=True)
class StageAnalysisResult:
    """Domain result of the stage analysis service.

    Carries region (``server``) + provenance on the facts (§V5) and the
    evidence-backed observations (§V6). ``status == "not_found"`` implies
    ``stage is None`` and empty occurrences/observations.
    """

    status: StageAnalysisStatus
    server: str
    stage: StageFacts | None
    occurrences: tuple[EnemyOccurrenceFacts, ...]
    observations: tuple[Observation, ...]
    warnings: tuple[str, ...]
    analyzer_version: str | None
    #: §V102 (§T195): set when the requested ``stage_code`` matched more than one stage,
    #: so the tool can name the pick + its alternates instead of answering silently.
    ambiguity: StageAmbiguity | None = None
    #: §V101 (§T197): the stage-level scalars the lane/route + tiles/deploy rules decide
    #: from. They are emitted (as ``stage.metrics``) because the evidence rows name them
    #: as field paths, and a path a response never carries is not one a client can look
    #: up -- the same defect as the packed ``"def/res"`` pseudo-field (B137).
    metrics: StageMetrics | None = None


def _parse_damage_types(raw: str | None) -> tuple[str, ...] | None:
    """Decode ``enemies.damage_types_json`` preserving the §V26/§V67 missing/empty
    distinction the analyzer relies on: SQL ``NULL`` (or an undecodable fragment)
    -> ``None`` (field absent), ``"[]"`` -> ``()`` (present but empty). Decodes
    through the shared §V37 :func:`~arknights_mcp.util.coerce.json_load` home; only
    the list/str shaping the analyzer needs is applied on top. 42 real enemies deal
    both ``PHYSIC`` and ``MAGIC``, which is why the fact is a list at all."""
    data = json_load(raw)
    if not isinstance(data, list):
        return None
    return tuple(str(token) for token in data)


def _stage_facts(stage: StageRow) -> StageFacts:
    """Shape a repository row into the typed, region-attributed facts (§V5/§V37).

    Single home for the ``StageRow -> StageFacts`` mapping shared by
    :func:`analyze_stage` and :func:`get_stage`; the two joins backing ``stage``
    are NOT NULL, so ``provenance`` (snapshot_id + imported_at) is always present.
    """
    return StageFacts(
        server=stage.server,
        game_id=stage.game_id,
        stage_code=stage.stage_code,
        display_name=stage.display_name,
        zone_game_id=stage.zone_game_id,
        zone_display_name=stage.zone_display_name,
        event_name=stage.event_name,
        stage_type=stage.stage_type,
        # §V80/B84: the emitted ``difficulty`` is the truthful stage-variant tag, not
        # the raw source column -- a ``tough_*`` / ``easy_*`` game_id is upgraded off
        # ``NORMAL`` through the one §V37 home shared with the search locators.
        difficulty=stage_variant(stage.game_id, stage.difficulty),
        sanity_cost=stage.sanity_cost,
        recommended_level=stage.recommended_level,
        max_life_points=stage.max_life_points,
        provenance=StageProvenance(snapshot_id=stage.snapshot_id, imported_at=stage.imported_at),
    )


#: §V102/§V19 (§T195): the bounded read behind an ambiguous ``stage_code``. The whole
#: matching set is read so the pick can be disclosed with its alternates, but the read
#: stays bounded -- never an unbounded slice (§V19). 64 is set well above the real
#: maximum, COUNTED rather than guessed (§V96): the largest group on the 2026-07-28
#: build is 36 stages (``LT-1``..``LT-6``, en + cn). A set that ever hits the cap is
#: reported as truncated rather than silently undercounted (§V74 (a) precedent).
MAX_STAGE_CODE_MATCHES = 64


@dataclass(frozen=True)
class StageAmbiguity:
    """The disclosure owed when a ``stage_code`` selected more than one stage (§V102).

    ``stage_code`` is unique to no one: on the 2026-07-28 build 927 en codes (2293
    stages) are shared, so ``get_stage(stage_code="4-4")`` answered with ``main_04-04``
    and never mentioned ``main_04-04#f#`` -- the client believed it asked about "4-4"
    and got "4-4" (B139). This carries what the tool layer needs to say so: the code
    asked for, the stage actually answered with (its ``game_id`` + its §V80-truthful
    ``difficulty``), and the alternates' ``game_id``s -- the only handle that can select
    one of them. TYPED data only; the client-facing wording lives in one home at the
    tool layer (§V37/§V71).

    ``truncated`` marks a matching set that hit :data:`MAX_STAGE_CODE_MATCHES`, so a
    partial alternates list is never presented as complete (§V26).
    """

    stage_code: str
    chosen_game_id: str
    chosen_difficulty: str | None
    alternates: tuple[str, ...]
    truncated: bool


def _resolve_stage(
    repo: StageRepository,
    server: str,
    *,
    stage_code: str | None,
    game_id: str | None,
) -> tuple[StageRow | None, StageAmbiguity | None]:
    """Resolve a stage by ``game_id`` (preferred, unique) or ``stage_code`` (§V37).

    Single home for the selector shared by :func:`analyze_stage`, :func:`get_stage`
    and :func:`~arknights_mcp.services.drops.get_stage_drops`. Raises
    :class:`ValueError` when neither is given (the tool models require exactly one, but
    a direct caller must fail loudly, not silently).

    Returns ``(stage, ambiguity)``. ``game_id`` is the unique key, so it never yields an
    ambiguity. A ``stage_code`` matching several stages resolves DETERMINISTICALLY to
    the lowest ``stage_pk`` -- the same stage every run (§V91) and the same one this
    lookup has always returned (§V21: the pick does not change, only the disclosure is
    added) -- and reports the rest as :class:`StageAmbiguity` so the caller can name the
    pick and its alternates instead of answering half the question silently (§V102 b).
    """
    if game_id is not None:
        return repo.stage_by_game_id(server, game_id), None
    if stage_code is None:
        raise ValueError("stage lookup requires stage_code or game_id")
    matches = repo.stages_by_code(server, stage_code, MAX_STAGE_CODE_MATCHES)
    if not matches:
        return None, None
    chosen = matches[0]
    if len(matches) == 1:
        return chosen, None
    return chosen, StageAmbiguity(
        stage_code=stage_code,
        chosen_game_id=chosen.game_id,
        # §V80/B84: the truthful variant tag, through the same §V37 home the facts use --
        # the disclosure must not say NORMAL about a stage the facts call FOUR_STAR.
        chosen_difficulty=stage_variant(chosen.game_id, chosen.difficulty),
        alternates=tuple(row.game_id for row in matches[1:]),
        truncated=len(matches) == MAX_STAGE_CODE_MATCHES,
    )


def _not_found(server: str) -> StageAnalysisResult:
    return StageAnalysisResult(
        status="not_found",
        server=server,
        stage=None,
        occurrences=(),
        observations=(),
        warnings=(),
        analyzer_version=None,
    )


def analyze_stage(
    conn: sqlite3.Connection,
    *,
    server: str,
    stage_code: str | None = None,
    game_id: str | None = None,
) -> StageAnalysisResult:
    """Analyze one stage for ``server``, selected by ``game_id`` (preferred, the
    unique key) or ``stage_code``. Read-only; parameterized SQL only (§V2).

    Returns a :class:`StageAnalysisResult` with region + provenance on the facts
    (§V5) and the analyzer's evidence-backed observations (§V6). Both transports
    call this same function (§V14).
    """
    repo = StageRepository(conn)
    stage, ambiguity = _resolve_stage(repo, server, stage_code=stage_code, game_id=game_id)

    if stage is None:
        return _not_found(server)

    facts = _stage_facts(stage)

    occurrences: list[EnemyOccurrenceFacts] = []
    threat_inputs: list[EnemyOccurrence] = []
    for enemy in repo.stage_enemies(stage.stage_pk):
        # §V37: decoded once and handed to BOTH the facts row and the rule input, so the
        # evidence path and the value it names can never be decoded two different ways.
        damage_types = _parse_damage_types(enemy.damage_types_json)
        occurrences.append(
            EnemyOccurrenceFacts(
                game_id=enemy.game_id,
                display_name=enemy.display_name,
                enemy_class=enemy.enemy_class,
                is_boss=enemy.is_boss,
                is_elite=enemy.is_elite,
                motion_type=enemy.motion_type,
                attack_type=enemy.attack_type,
                damage_types=damage_types,
                level_variant=enemy.level_variant,
                total_count=enemy.total_count,
                first_spawn_time=enemy.first_spawn_time,
                last_spawn_time=enemy.last_spawn_time,
                route_count=enemy.route_count,
                hp=enemy.hp,
                atk=enemy.atk,
                def_=enemy.def_,
                res=enemy.res,
                attack_interval=enemy.attack_interval,
                move_speed=enemy.move_speed,
                weight=enemy.weight,
                variant_id=enemy.variant_id,
                # §V101/§T197: the fields the ranged-arts rule cites as evidence paths,
                # so those paths resolve on this response.
                attack_range=enemy.attack_range,
                targeting=enemy.targeting,
            )
        )
        threat_inputs.append(
            EnemyOccurrence(
                game_id=enemy.game_id,
                display_name=enemy.display_name,
                motion_type=enemy.motion_type,
                damage_types=damage_types,
                total_count=enemy.total_count,
                defense=enemy.def_,
                res=enemy.res,
                attack_range=enemy.attack_range,
                targeting=enemy.targeting,
                first_spawn_time=enemy.first_spawn_time,
                last_spawn_time=enemy.last_spawn_time,
                route_count=enemy.route_count,
            )
        )

    # Stage-level rule inputs (§T39): distinct routes + the deploy-tile summary. A
    # tile-less stage passes ``tiles=None`` so the tiles/deploy rule skips it (§V26).
    total_tiles, buildable_melee, buildable_ranged = repo.tile_summary(stage.stage_pk)
    tiles = (
        StageTiles(
            total=total_tiles,
            buildable_melee=buildable_melee,
            buildable_ranged=buildable_ranged,
        )
        if total_tiles > 0
        else None
    )

    route_count = repo.route_count(stage.stage_pk)
    analysis = run_threat_analysis(
        StageThreatContext(
            server=stage.server,
            # §V68/B136: the analyzer refs evidence by the stage's unique game_id --
            # stage_code is shared by the normal/tough variants and rides along for
            # display only.
            stage_game_id=stage.game_id,
            stage_code=stage.stage_code,
            occurrences=tuple(threat_inputs),
            route_count=route_count,
            tiles=tiles,
        )
    )

    # §V101: the same stage-level scalars the rules just decided from, carried out so the
    # evidence rows that name them resolve against the response (§V67: absent, not zero).
    metrics = StageMetrics(
        route_record_count=route_count,
        tile_total=tiles.total if tiles is not None else None,
        buildable_melee=tiles.buildable_melee if tiles is not None else None,
        buildable_ranged=tiles.buildable_ranged if tiles is not None else None,
    )

    return StageAnalysisResult(
        status="ok",
        server=stage.server,
        stage=facts,
        occurrences=tuple(occurrences),
        observations=analysis.observations,
        warnings=analysis.warnings,
        analyzer_version=analysis.analyzer_version,
        ambiguity=ambiguity,
        metrics=None if metrics.is_empty() else metrics,
    )


# --- get_stage (§T34): facts by default; heavy map/routes/spawns opt-in + paged.


@dataclass(frozen=True)
class SectionPage:
    """Bounded page descriptor for one opt-in section (§V19/§V22).

    ``total`` is the full row count; ``has_more`` signals another bounded page
    (never an invitation to dump). Mirrors
    :class:`~arknights_mcp.models.common.PageInfo` on the wire.
    """

    page: int
    page_size: int
    total: int
    has_more: bool


@dataclass(frozen=True)
class StageMapFacts:
    """The stage's map header; tiles are delivered as a separate paged section."""

    width: int | None
    height: int | None
    map_version: str | None
    environment: object | None


@dataclass(frozen=True)
class SpawnFacts:
    """One scheduled spawn on the stage timeline (typed structural fields only).

    ``variant_id`` is the inline-variant id for a ``useDb:false`` spawn (§T80);
    ``enemy_game_id`` stays the base prefab, so a client can see both.
    """

    wave_index: int
    enemy_game_id: str
    enemy_level_variant: int | None
    route_index: int | None
    spawn_time: float | None
    count: int | None
    interval: float | None
    spawn_group: str | None
    hidden: bool
    variant_id: str | None


@dataclass(frozen=True)
class StageDetailResult:
    """Domain result of :func:`get_stage`.

    Carries region + provenance on ``stage`` (§V5). The heavy sections are
    populated only when their include flag is set; ``routes`` and ``spawns`` each
    pair their rows with a bounded :class:`SectionPage` (§V19/§V22). ``tile_grid``
    is the compact per-row grid encoding (§V74 (c)) -- a whole board fits one
    response, so it carries no page cursor; an over-budget board yields
    ``tile_grid=None`` plus a §V22 caption in ``limitations``. ``map_image`` is the
    render-own SVG of the stage grid (§T122), populated only when
    ``include_map_image`` is set and the board renders within the §V22 budget;
    ``limitations`` carries any §V22 caption (e.g. an over-budget map image was
    omitted). ``status == "not_found"`` implies every section is empty/``None``.
    """

    status: StageAnalysisStatus
    server: str
    stage: StageFacts | None
    stage_map: StageMapFacts | None
    tile_grid: TileGridFacts | None
    routes: tuple[RouteFacts, ...]
    routes_page: SectionPage | None
    spawns: tuple[SpawnFacts, ...]
    spawns_page: SectionPage | None
    map_image: RenderedMap | None = None
    limitations: tuple[str, ...] = ()
    #: §V102 (§T195): set when the requested ``stage_code`` matched more than one stage.
    ambiguity: StageAmbiguity | None = None


def _validate_page(page: int, page_size: int) -> tuple[int, int]:
    """Reject out-of-range pagination -- never silently widen it (§V19).

    Mirrors :class:`~arknights_mcp.models.common.PageParams` (``page >= 1``,
    ``1 <= page_size <= PAGE_SIZE_MAX``): the model is the MCP gate, but a caller
    reaching this service directly (or a transport skipping model validation) must
    get the *same* rejection, not a silent clamp -- one §V19 contract, both places.
    """
    p, size = int(page), int(page_size)
    if p < 1:
        raise ValueError(f"page {p} must be >= 1 (§V19)")
    if size < 1 or size > PAGE_SIZE_MAX:
        raise ValueError(f"page_size {size} outside the §V19 window [1, {PAGE_SIZE_MAX}]")
    return p, size


def _section_page(page: int, page_size: int, total: int) -> SectionPage:
    """Build the §V19 page descriptor. ``has_more`` is purely count-derived."""
    return SectionPage(
        page=page,
        page_size=page_size,
        total=total,
        has_more=page * page_size < total,
    )


def _not_found_detail(server: str) -> StageDetailResult:
    return StageDetailResult(
        status="not_found",
        server=server,
        stage=None,
        stage_map=None,
        tile_grid=None,
        routes=(),
        routes_page=None,
        spawns=(),
        spawns_page=None,
    )


def _build_map_image(
    repo: StageRepository,
    stage_pk: int,
    raw_map: StageMapRow | None,
    route_rows: list[StageRouteRow],
) -> tuple[RenderedMap | None, str | None]:
    """Render the stage grid into a bounded SVG (§T122; §V16/§V22).

    Reads the full grid (each read bounded by the render cap so an oversized table
    is never loaded whole, §V22), adapts the repository rows + the caller's
    already-read route rows (one bounded route read shared with the digest, §V37)
    into the render's plain value objects, and returns ``(image, limitation)``: an
    in-budget board yields the image; an over-budget one yields no image + a §V22
    caption. The image is a DERIVED render from typed grid data -- no third-party
    art byte and no imported source string reaches it (§V16/§V18)."""
    cells = [
        MapCell(
            x=t.x,
            y=t.y,
            height_type=t.height_type,
            buildable_type=t.buildable_type,
            passable=t.passable,
            tile_key=t.tile_key,
        )
        for t in repo.all_tiles(stage_pk, MAX_MAP_CELLS + 1)
    ]
    routes = [
        MapRoute(
            start=_point_xy(json_load(r.start_position_json)),
            end=_point_xy(json_load(r.end_position_json)),
            checkpoints=_checkpoint_points(json_load(r.checkpoints_json)),
        )
        for r in route_rows
    ]
    result = render_stage_map(
        width=raw_map.width if raw_map else None,
        height=raw_map.height if raw_map else None,
        cells=cells,
        routes=routes,
    )
    return result.image, result.limitation


def get_stage(
    conn: sqlite3.Connection,
    *,
    server: str,
    stage_code: str | None = None,
    game_id: str | None = None,
    include_map: bool = False,
    include_routes: bool = False,
    include_spawns: bool = False,
    include_map_image: bool = False,
    routes_page: int = 1,
    routes_page_size: int = PAGE_SIZE_DEFAULT,
    spawns_page: int = 1,
    spawns_page_size: int = PAGE_SIZE_DEFAULT,
) -> StageDetailResult:
    """Fetch one stage's facts + optional map/routes/spawns (§T34; §V5/§V19/§V22).

    Read-only; parameterized SQL only (§V2). The default response is facts +
    provenance only (§V22 -- the heavy sections stay off). ``include_map`` returns
    the tile grid as one compact per-row block (§V74 (c), no page cursor -- a whole
    board fits one response); ``include_routes``/``include_spawns`` each page
    through their **own** cursor (``routes_page``/``spawns_page``), so a client can
    request several sections at once and still page a large one without shifting the
    others off, and no included payload ever yields an unbounded slice (§V19). A
    tile board larger than the §V22 cap yields no grid + a limitation.
    ``include_map_image`` (off by default, §V22) adds a render-own SVG of the stage
    grid (§T122) -- a DERIVED image drawn from the stored typed grid data, never
    third-party art (§V16) and never the §V63 URL reference; an over-budget board is
    omitted with a §V22 limitation. Every cursor is validated against the §V19
    window here too, mirroring the model gate. Both transports call this function
    (§V14).
    """
    rp, rsize = _validate_page(routes_page, routes_page_size)
    sp, ssize = _validate_page(spawns_page, spawns_page_size)
    repo = StageRepository(conn)
    stage, ambiguity = _resolve_stage(repo, server, stage_code=stage_code, game_id=game_id)
    if stage is None:
        return _not_found_detail(server)

    stage_pk = stage.stage_pk
    limitations: list[str] = []

    # The map header is read once and shared by the tile grid and the render-own
    # image (§V37), so a request for both does not query it twice.
    raw_map: StageMapRow | None = None
    if include_map or include_map_image:
        raw_map = repo.stage_map(stage_pk)

    stage_map: StageMapFacts | None = None
    tile_grid: TileGridFacts | None = None
    if include_map:
        # Read the full grid, bounded by the §V22 cap so an oversized table is never
        # loaded whole (the +1 detects the over-cap case). The compact per-row
        # encoding makes a whole board fit one response, so tiles are not paged
        # (§V74 (c)).
        tile_rows = repo.all_tiles(stage_pk, MAX_MAP_CELLS + 1)
        # Only surface a map section when there is one; an all-null header with no
        # tiles is indistinguishable from "map absent", so omit it instead (the
        # tool then emits no ``map`` key at all).
        if raw_map is not None or tile_rows:
            stage_map = StageMapFacts(
                width=raw_map.width if raw_map else None,
                height=raw_map.height if raw_map else None,
                map_version=raw_map.map_version if raw_map else None,
                environment=json_load(raw_map.environment_json) if raw_map else None,
            )
            # §V74 (c)/§V26: resolve the grid and, when a non-empty board is over the
            # count cap or refused (over-extent / too many types), the say-so limitation
            # (B70/B71) so a refused grid is never a silent None read as "no tiles".
            tile_grid, grid_limitation = resolve_tile_grid(
                tile_rows,
                raw_map.width if raw_map else None,
                raw_map.height if raw_map else None,
            )
            if grid_limitation is not None:
                limitations.append(grid_limitation)

    # §V96/B128: ONE bounded route read (§V22) shared by the digest and the render
    # (§V37 -- and no double read when both flags are set); a checkpoint `type`
    # outside the known census stays on the conservative spatial side with a say-so,
    # never a silent bucket (§V26).
    route_rows: list[StageRouteRow] = []
    if include_routes or include_map_image:
        route_rows = repo.all_routes(stage_pk, MAX_MAP_ROUTES)
        unknown_limitation = unknown_checkpoint_type_limitation(route_rows)
        if unknown_limitation is not None:
            limitations.append(unknown_limitation)

    routes: tuple[RouteFacts, ...] = ()
    routes_page_info: SectionPage | None = None
    if include_routes:
        # §V74 (a)/B21: digest the FULL route set to distinct geometry BEFORE paging,
        # so page 1 is the first N distinct routes and the total is stable across
        # pages (a per-page dedup would split one geometry across a page boundary).
        # §V74 (a)/B73: a raw count equal to the cap means records past it were dropped
        # BEFORE the dedup, so the distinct total may under-report -- say so rather than
        # silently undercount (§V26). Detection is raw-count == cap per §V74 (a).
        if len(route_rows) == MAX_MAP_ROUTES:
            limitations.append(_route_truncated_limitation())
        distinct = _distinct_routes(route_rows)
        offset = (rp - 1) * rsize
        routes = tuple(distinct[offset : offset + rsize])
        routes_page_info = _section_page(rp, rsize, len(distinct))

    spawns: tuple[SpawnFacts, ...] = ()
    spawns_page_info: SectionPage | None = None
    if include_spawns:
        offset = (sp - 1) * ssize
        spawns = tuple(
            SpawnFacts(
                wave_index=s.wave_index,
                enemy_game_id=s.enemy_game_id,
                enemy_level_variant=s.enemy_level_variant,
                route_index=s.route_index,
                spawn_time=s.spawn_time,
                count=s.count,
                interval=s.interval,
                spawn_group=s.spawn_group,
                hidden=s.hidden,
                variant_id=s.variant_id,
            )
            for s in repo.spawns(stage_pk, ssize, offset)
        )
        spawns_page_info = _section_page(sp, ssize, repo.spawn_count(stage_pk))

    map_image: RenderedMap | None = None
    if include_map_image:
        map_image, image_limitation = _build_map_image(repo, stage_pk, raw_map, route_rows)
        if image_limitation is not None:
            limitations.append(image_limitation)

    return StageDetailResult(
        status="ok",
        server=stage.server,
        stage=_stage_facts(stage),
        stage_map=stage_map,
        tile_grid=tile_grid,
        routes=routes,
        routes_page=routes_page_info,
        spawns=spawns,
        spawns_page=spawns_page_info,
        map_image=map_image,
        limitations=tuple(limitations),
        ambiguity=ambiguity,
    )
