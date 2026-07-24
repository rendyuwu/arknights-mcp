"""Render-own stage-map image service (§T122; §V16/§V22/§C).

The single home (§V37) for turning a stage's already-stored, typed grid data
(``stage_maps`` width/height + ``stage_tiles`` + ``stage_routes``) into a small,
self-contained **SVG document** -- a DERIVED WORK, drawn from primitives we
generate, that embeds zero third-party art bytes.

Three invariants hold **by construction** here:

* **§V16 (derived work, no art bytes).** The output is a plain SVG built from our
  own geometric primitives (a board rect, grid lines, one rect per tile coloured
  by its typed category, and route start/end/checkpoint markers). It embeds no
  ``<image>`` element, no external ``href``/``xlink:href``, and no link to the
  ``yuanyan3060`` mirror -- this is explicitly NOT the §V63 URL-reference path.
  No imported source string is ever interpolated into the document: tile fills
  are chosen from the typed ``height_type``/``buildable_type``/``passable`` fields
  via a fixed colour map, and only integer coordinates + our own fixed literals
  reach the markup, so an untrusted tile string cannot smuggle bytes into the
  image (§V18 by omission).
* **§V22 (bounded, opt-in).** Rendering is opt-in at the tool layer (off by
  default). The render is doubly bounded here: a board larger than
  :data:`MAX_MAP_CELLS` cells, or a document that would exceed
  :data:`MAX_MAP_IMAGE_BYTES`, is refused -- :func:`render_stage_map` returns no
  image and a limitation instead, so an oversized map degrades to a caption rather
  than an oversized payload. The envelope's own §V22 cap remains the final
  backstop.
* **§C (no new dependency).** The SVG is assembled as a pure-Python string; no
  raster/imaging library is imported. Output is deterministic (tiles are drawn in
  a fixed ``(y, x)`` order, colours are fixed literals), so the same grid always
  renders byte-identical.

The renderer takes plain value objects (:class:`MapCell` / :class:`MapRoute`), not
repository rows, so it has no dependency on the DB layer and is trivially unit
tested; the stage service adapts its rows into these.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

#: §V22 upper bound on the board size we will render, in grid cells
#: (``width * height``) or stored tiles, whichever is larger. A real stage grid is
#: at most a few hundred cells; this generous cap refuses only a pathological board
#: before a huge string is ever built. Single §V37 home -- the stage service reads
#: it to bound its tile read so an oversized table is never loaded whole.
MAX_MAP_CELLS = 4_000

#: §V22 upper bound on the number of routes drawn. Routes are few (tens) per stage;
#: this bounds the marker overlay so it cannot balloon the document.
MAX_MAP_ROUTES = 1_000

#: §V22 byte budget for one rendered map image, measured on the image's *wire*
#: size -- the JSON-escaped bytes it contributes to the envelope (see
#: :func:`_wire_bytes`), the same ``ensure_ascii`` measure the envelope cap uses
#: (``envelopes.serialized_size``). The SVG is emitted as a JSON string value, and
#: its many attribute quotes each escape to ``\\"``, so its raw UTF-8 length
#: understates its wire size by ~15-20%; budgeting on the wire size keeps the image
#: truly below the 200 KB envelope cap. Set below that cap so an over-budget image is
#: dropped *here* (with a limitation, the rest of the response intact) rather than
#: tripping the envelope cap and withholding the whole payload. Single §V37 home.
MAX_MAP_IMAGE_BYTES = 128_000

#: SVG media type for the derived document (§T122). Inline ``image/svg+xml`` -- an
#: image content payload, NOT a URL reference (§V63 is a different path).
SVG_MEDIA_TYPE = "image/svg+xml"

# Fixed layout constants (our own, deterministic). Pixels per grid cell + outer pad.
_CELL_PX = 24
_PAD_PX = 8

# Fixed fill palette keyed on TYPED tile fields only (§V16/§V26 discipline): no
# imported string reaches the document, only these literals we author.
_FILL_WALL = "#546e7a"  # impassable void/blocked, non-deployable, off the enemy path
_FILL_RANGED = "#a5d6a7"  # buildable HIGHLAND (ranged platform)
_FILL_MELEE = "#90caf9"  # buildable LOWLAND (melee ground)
_FILL_ROAD = "#cfd8dc"  # enemy-path tile (tile_road/start/end/flystart ...)
_FILL_FORBIDDEN = "#eceff1"  # passable-but-non-buildable, non-path (tile_forbidden ...)
_STROKE_BOARD = "#37474f"
_STROKE_GRID = "#b0bec5"
_FILL_BACKDROP = "#fafafa"
_MARK_START = "#2e7d32"  # route entry (enemy spawn)
_MARK_END = "#c62828"  # route exit (objective)
_MARK_PATH = "#ef6c00"  # checkpoint polyline

#: Typed source ``tileKey`` values naming a tile the enemy route runs over -- the
#: enemy PATH. A road that is ALSO melee-buildable stays a road here so its enemy-path
#: meaning is not hidden behind a deploy-surface colour (§V82/B86: a melee-deploy road
#: must not read as plain "buildable ground"). Read of the typed field only (§V26); an
#: unknown key falls through to the buildable/passable classification below.
_ROAD_TILE_KEYS = frozenset(
    {"tile_road", "tile_start", "tile_end", "tile_flystart", "tile_telin", "tile_telout"}
)

#: §T140 (B65)/§T166 (B86) canonical colour -> client-facing meaning for the derived
#: map's fixed palette, so a client can decode the opaque hex fills/markers a rendered
#: image carries. Keyed on the SAME typed tile_key/buildable/passable semantics the
#: compact tile-grid legend exposes (§V82) -- the single §V37 home for the colour
#: meanings, read by both :func:`_tile_fill` and the per-render legend. Meanings are
#: plain client-facing text -- no spec cites or internal jargon (§V71).
_LEGEND_MEANINGS: dict[str, str] = {
    _FILL_WALL: "impassable -- blocked or void tile; not deployable and not on the enemy path",
    _FILL_MELEE: "buildable ground -- deploy melee (blocking) operators here",
    _FILL_RANGED: "buildable highland -- deploy ranged operators here",
    _FILL_ROAD: "enemy path -- enemies advance along these tiles",
    _FILL_FORBIDDEN: "non-deployable tile -- cannot deploy here and not on the enemy path",
    _MARK_START: "route start -- where enemies spawn",
    _MARK_END: "route end -- the objective enemies march toward",
    _MARK_PATH: "enemy route path",
}

#: Fixed legend emission order (single §V37 home). A per-render legend lists ONLY the
#: colours actually drawn (§V82/B86), in this order.
_LEGEND_ORDER: tuple[str, ...] = (
    _FILL_WALL,
    _FILL_MELEE,
    _FILL_RANGED,
    _FILL_ROAD,
    _FILL_FORBIDDEN,
    _MARK_START,
    _MARK_END,
    _MARK_PATH,
)

#: The full palette legend (every colour, in order) -- documentation of the whole map,
#: not what any one render ships. A rendered image carries :func:`_legend_for`, the
#: present-colours subset (§V82). Surfaced on the wire as ``map_image.legend``.
MAP_LEGEND: tuple[dict[str, str], ...] = tuple(
    {"color": color, "meaning": _LEGEND_MEANINGS[color]} for color in _LEGEND_ORDER
)


def _legend_for(colors: set[str]) -> tuple[dict[str, str], ...]:
    """The legend for the colours a render actually emits, in canonical order (§V82).

    §V82/B86: the legend lists ONLY the fills/markers present in THIS render -- a colour
    no tile or route drew (an "impassable" swatch on a board with no walls) is never
    listed. Ordered by :data:`_LEGEND_ORDER` so the legend stays deterministic (§C)."""
    return tuple(
        {"color": color, "meaning": _LEGEND_MEANINGS[color]}
        for color in _LEGEND_ORDER
        if color in colors
    )


@dataclass(frozen=True)
class MapCell:
    """One tile of the stage grid, reduced to the fields the render keys on (§T122).

    ``x``/``y`` are the stored grid coordinates; ``height_type``/``buildable_type``/
    ``tile_key`` are the typed source enums the fill colour is chosen from (the same
    fields the compact tile-grid legend exposes, §V82); ``passable`` walls a cell when
    explicitly ``False``. ``tile_key`` distinguishes an enemy-path tile (a road) from a
    non-buildable non-path tile (``tile_forbidden``) so neither is mislabelled (§V82/B86).
    No field is emitted verbatim into the SVG.
    """

    x: int
    y: int
    height_type: str | None
    buildable_type: str | None
    passable: bool | None
    tile_key: str | None = None


@dataclass(frozen=True)
class MapRoute:
    """One enemy route as grid points (§T122).

    ``start``/``end`` are ``(x, y)`` grid coordinates (the stored ``{col, row}``
    normalised to ``(col, row)`` by the caller) or ``None`` when absent;
    ``checkpoints`` is the ordered intermediate path.
    """

    start: tuple[int, int] | None
    end: tuple[int, int] | None
    checkpoints: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class RenderedMap:
    """A rendered stage-map image for the wire (§T122).

    ``svg`` is the self-contained document (an inline image content payload, not a
    URL reference -- §V63 is a different path); ``media_type`` is
    :data:`SVG_MEDIA_TYPE`. ``pixel_width``/``pixel_height`` are the document
    viewport; ``tile_count`` is how many stored tiles were drawn. ``legend`` maps the
    image's fixed colours to plain client-facing meanings so a client can decode the
    opaque fills the SVG carries (B65) -- and lists ONLY the colours THIS render
    actually emits (§V82/B86, :func:`_legend_for`).
    """

    media_type: str
    svg: str
    pixel_width: int
    pixel_height: int
    tile_count: int
    legend: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class RenderResult:
    """Outcome of :func:`render_stage_map`.

    Exactly one of ``image`` / ``limitation`` is meaningful for a stage with grid
    data: an in-budget render yields ``image`` (``limitation`` ``None``); an
    over-budget board yields no ``image`` + a §V22 ``limitation`` caption. A stage
    with no grid data at all yields both ``None`` (nothing to render, nothing to
    caption).
    """

    image: RenderedMap | None = None
    limitation: str | None = None


def _wire_bytes(svg: str) -> int:
    """Wire byte size the SVG contributes to the envelope (matches the §V22 cap).

    The SVG is serialized as a JSON string value in the response envelope, so its
    on-the-wire size is the JSON-escaped, ``ensure_ascii`` byte length -- every
    attribute quote becomes ``\\"`` (2 bytes), inflating the raw UTF-8 length by
    ~15-20%. Measuring against this (rather than ``svg.encode("utf-8")``) is the
    same measure ``envelopes.serialized_size`` applies to the whole envelope, so the
    image budget stays a true fraction of the 200 KB cap. ``json.dumps`` wraps the
    value in two extra quote bytes -- a negligible, fail-closed over-count.
    """
    return len(json.dumps(svg).encode("utf-8"))


def _oversize_limitation() -> str:
    """The single §V37 home for the §V22 over-budget caption."""
    kib = MAX_MAP_IMAGE_BYTES // 1000
    return (
        f"map image omitted: the rendered stage map exceeds the {kib} KB image "
        "budget; use include_map for the compact tile grid instead"
    )


def _tile_fill(cell: MapCell) -> str:
    """Pick a fixed fill for a tile from its TYPED fields only (§V16/§V26/§V82).

    Keyed on the same tile_key/buildable/passable semantics the compact tile-grid
    legend exposes (§V82), so the render and the grid agree. Order of precedence:

    * an enemy-path tile (``tile_key`` in :data:`_ROAD_TILE_KEYS`) is a ROAD -- even
      when it is also melee-buildable, so a melee-deploy road never reads as plain
      "buildable ground" (§V82/B86);
    * a buildable cell is a deploy surface (ranged on HIGHLAND / explicit RANGED, else
      melee) -- checked before ``passable`` so a highland ranged platform (buildable but
      not ground-passable) is a deploy tile, not a wall;
    * an explicitly non-passable, non-buildable cell is an impassable wall;
    * anything else (a passable, non-buildable, non-path tile such as ``tile_forbidden``)
      is a non-deployable tile, NOT a "walkable path" (§V82/B86).

    Unknown/absent enum values fall through -- the raw source string is never emitted,
    only these authored literals.
    """
    if (cell.tile_key or "").lower() in _ROAD_TILE_KEYS:
        return _FILL_ROAD
    buildable = (cell.buildable_type or "").upper()
    if buildable not in ("", "NONE"):
        highland = (cell.height_type or "").upper() == "HIGHLAND"
        return _FILL_RANGED if (highland or buildable == "RANGED") else _FILL_MELEE
    if cell.passable is False:
        return _FILL_WALL
    return _FILL_FORBIDDEN


def _effective_extent(
    width: int | None,
    height: int | None,
    cells: Sequence[MapCell],
    routes: Sequence[MapRoute],
) -> tuple[int, int]:
    """Resolve the board extent in cells (§T122).

    Prefer the stored ``stage_maps`` ``width``/``height``; when a dimension is
    missing or non-positive, derive it from the maximum tile/route coordinate so a
    grid with only tiles still renders. Returns ``(0, 0)`` when there is nothing to
    render.
    """
    xs: list[int] = [c.x for c in cells]
    ys: list[int] = [c.y for c in cells]
    for route in routes:
        for point in (route.start, route.end, *route.checkpoints):
            if point is not None:
                xs.append(point[0])
                ys.append(point[1])
    eff_w = width if (width is not None and width > 0) else (max(xs) + 1 if xs else 0)
    eff_h = height if (height is not None and height > 0) else (max(ys) + 1 if ys else 0)
    return max(eff_w, 0), max(eff_h, 0)


def _cell_center(x: int, y: int, eff_h: int) -> tuple[int, int]:
    """Pixel centre of grid cell ``(x, y)``.

    The game grid has ``y`` growing upward while SVG ``y`` grows downward, so the
    row is flipped (``eff_h - 1 - y``) to render the board right-way-up.
    """
    cx = _PAD_PX + x * _CELL_PX + _CELL_PX // 2
    cy = _PAD_PX + (eff_h - 1 - y) * _CELL_PX + _CELL_PX // 2
    return cx, cy


def _cell_origin(x: int, y: int, eff_h: int) -> tuple[int, int]:
    """Top-left pixel of grid cell ``(x, y)`` (row flipped like :func:`_cell_center`)."""
    px = _PAD_PX + x * _CELL_PX
    py = _PAD_PX + (eff_h - 1 - y) * _CELL_PX
    return px, py


def _draw_grid_lines(eff_w: int, eff_h: int) -> list[str]:
    """The board backdrop + grid lines (cheap: ``eff_w + eff_h + 2`` lines)."""
    board_w = eff_w * _CELL_PX
    board_h = eff_h * _CELL_PX
    parts: list[str] = [
        f'<rect class="board" x="{_PAD_PX}" y="{_PAD_PX}" width="{board_w}" '
        f'height="{board_h}" fill="{_FILL_BACKDROP}" stroke="{_STROKE_BOARD}"/>'
    ]
    for i in range(eff_w + 1):
        px = _PAD_PX + i * _CELL_PX
        parts.append(
            f'<line class="grid" x1="{px}" y1="{_PAD_PX}" x2="{px}" '
            f'y2="{_PAD_PX + board_h}" stroke="{_STROKE_GRID}"/>'
        )
    for j in range(eff_h + 1):
        py = _PAD_PX + j * _CELL_PX
        parts.append(
            f'<line class="grid" x1="{_PAD_PX}" y1="{py}" '
            f'x2="{_PAD_PX + board_w}" y2="{py}" stroke="{_STROKE_GRID}"/>'
        )
    return parts


def _draw_tiles(cells: Sequence[MapCell], eff_h: int) -> list[str]:
    """One rect per stored tile, coloured by its typed category, in ``(y, x)`` order."""
    parts: list[str] = []
    for cell in sorted(cells, key=lambda c: (c.y, c.x)):
        px, py = _cell_origin(cell.x, cell.y, eff_h)
        parts.append(
            f'<rect class="tile" x="{px}" y="{py}" width="{_CELL_PX}" '
            f'height="{_CELL_PX}" fill="{_tile_fill(cell)}"/>'
        )
    return parts


def _distinct_route_geometries(routes: Sequence[MapRoute]) -> list[MapRoute]:
    """Collapse routes to DISTINCT geometry (B65).

    A stage stores many route records that share identical start/end/checkpoint
    geometry (4-4: 26 records, ~4 distinct); drawing every record over-plots the
    overlay with dozens of coincident markers (52 start/end circles for ~4 real
    routes). Records with identical ``(start, end, checkpoints)`` collapse to one;
    first-occurrence order is kept so the render stays deterministic (§C).

    Non-spatial WAIT checkpoints are already filtered UPSTREAM by their typed ``type``
    field (:func:`~arknights_mcp.services.stages._is_wait_checkpoint`, §V74 (b)/B74)
    before a :class:`MapRoute` reaches the renderer, so the checkpoints here are real
    grid waypoints only -- a ``MOVE`` legitimately targeting corner ``(0, 0)`` is NOT
    discarded (the earlier render-layer position-cleaning dropped it, B74). This pure
    renderer therefore draws whatever points it is given (§V37: one WAIT home, upstream).
    """
    seen: set[tuple[object, object, tuple[tuple[int, int], ...]]] = set()
    distinct: list[MapRoute] = []
    for route in routes:
        key = (route.start, route.end, route.checkpoints)
        if key in seen:
            continue
        seen.add(key)
        distinct.append(route)
    return distinct


def _is_degenerate(route: MapRoute) -> bool:
    """A route whose start and end coincide with no checkpoints between them (§V82/B86).

    Rendered, it stacks the green start circle and the red end circle on one cell -- an
    artifact, not real geometry (the live 4-4 eval carried a ``start==end==(0, 0)``,
    0-checkpoint route). Such a route is dropped from the overlay so no stacked-marker
    artifact is drawn. A start==end route that still has checkpoints is a real loop and
    is kept."""
    return route.start is not None and route.start == route.end and not route.checkpoints


def _drawable_routes(routes: Sequence[MapRoute]) -> list[MapRoute]:
    """The routes actually drawn: DISTINCT geometry (B65) minus degenerate ones (§V82/B86).

    Single §V37 home for "which routes reach the overlay", so the drawn markers and the
    present-colours legend (:func:`_present_colors`) never diverge."""
    return [route for route in _distinct_route_geometries(routes) if not _is_degenerate(route)]


def _draw_route_markers(routes: Sequence[MapRoute], eff_h: int) -> list[str]:
    """Start/end markers + a checkpoint polyline for each already-drawable route.

    ``routes`` is the :func:`_drawable_routes` list (distinct geometry, degenerate routes
    already dropped), so a stage's many duplicate route records do not over-plot the
    overlay (B65) and a start==end 0-checkpoint route draws no stacked circles (§V82/B86).
    WAIT placeholders were already filtered upstream by their typed ``type`` field
    (§V74 (b)/B74) so the polyline follows real grid waypoints only."""
    parts: list[str] = []
    radius = max(_CELL_PX // 3, 2)
    for route in routes:
        if len(route.checkpoints) >= 2:
            points = " ".join(
                f"{cx},{cy}" for cx, cy in (_cell_center(x, y, eff_h) for x, y in route.checkpoints)
            )
            parts.append(
                f'<polyline class="route-path" points="{points}" fill="none" '
                f'stroke="{_MARK_PATH}"/>'
            )
        if route.start is not None:
            cx, cy = _cell_center(route.start[0], route.start[1], eff_h)
            parts.append(
                f'<circle class="route-start" cx="{cx}" cy="{cy}" r="{radius}" '
                f'fill="{_MARK_START}"/>'
            )
        if route.end is not None:
            cx, cy = _cell_center(route.end[0], route.end[1], eff_h)
            parts.append(
                f'<circle class="route-end" cx="{cx}" cy="{cy}" r="{radius}" fill="{_MARK_END}"/>'
            )
    return parts


def _present_colors(cells: Sequence[MapCell], drawable_routes: Sequence[MapRoute]) -> set[str]:
    """The set of fills/markers the render actually emits (§V82/B86, legend input).

    Read from the SAME classifiers the draw functions use -- :func:`_tile_fill` per tile
    and the drawn-marker conditions per drawable route -- so the legend lists exactly the
    colours on the board, never a swatch nothing drew (§V37: one classification home)."""
    colors: set[str] = {_tile_fill(cell) for cell in cells}
    for route in drawable_routes:
        if len(route.checkpoints) >= 2:
            colors.add(_MARK_PATH)
        if route.start is not None:
            colors.add(_MARK_START)
        if route.end is not None:
            colors.add(_MARK_END)
    return colors


def render_stage_map(
    *,
    width: int | None,
    height: int | None,
    cells: Sequence[MapCell],
    routes: Sequence[MapRoute] = (),
) -> RenderResult:
    """Render a stage's grid into a bounded, self-contained SVG (§T122; §V16/§V22/§C).

    Returns a :class:`RenderResult`: an in-budget render carries the
    :class:`RenderedMap`; a board over :data:`MAX_MAP_CELLS` cells or a document
    over :data:`MAX_MAP_IMAGE_BYTES` carries no image and a §V22 limitation caption;
    a stage with no grid data at all carries neither. The document embeds no
    third-party art bytes and no imported source string -- only our own primitives,
    integer coordinates, and fixed literals (§V16/§V18).
    """
    eff_w, eff_h = _effective_extent(width, height, cells, routes)
    if eff_w <= 0 or eff_h <= 0:
        # No grid data to draw -- not an error, just nothing to render (§V22).
        return RenderResult()

    # §V22 pre-build guard: refuse a pathological board before assembling a huge
    # string (the stage service also bounds its tile read by MAX_MAP_CELLS).
    if eff_w * eff_h > MAX_MAP_CELLS or len(cells) > MAX_MAP_CELLS:
        return RenderResult(limitation=_oversize_limitation())

    pixel_width = eff_w * _CELL_PX + 2 * _PAD_PX
    pixel_height = eff_h * _CELL_PX + 2 * _PAD_PX

    # Resolve the routes actually drawn ONCE (distinct geometry, degenerate dropped) so
    # the overlay and the present-colours legend agree (§V37/§V82).
    drawable_routes = _drawable_routes(routes)
    body: list[str] = _draw_grid_lines(eff_w, eff_h)
    body += _draw_tiles(cells, eff_h)
    body += _draw_route_markers(drawable_routes, eff_h)

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{pixel_width}" '
        f'height="{pixel_height}" viewBox="0 0 {pixel_width} {pixel_height}" '
        'role="img">'
        "<title>Arknights stage map (derived render)</title>" + "".join(body) + "</svg>"
    )

    # §V22 byte budget: an over-budget document is dropped here (with a caption) so
    # the rest of the response survives, rather than tripping the envelope cap.
    # Measured on the JSON-escaped WIRE size (the bytes the SVG contributes to the
    # envelope), not raw UTF-8 -- the envelope cap counts the same way, so the budget
    # stays a true fraction of the 200 KB cap despite quote escaping.
    if _wire_bytes(svg) > MAX_MAP_IMAGE_BYTES:
        return RenderResult(limitation=_oversize_limitation())

    return RenderResult(
        image=RenderedMap(
            media_type=SVG_MEDIA_TYPE,
            svg=svg,
            pixel_width=pixel_width,
            pixel_height=pixel_height,
            tile_count=len(cells),
            # §V82/B86: legend lists ONLY the colours this render actually drew.
            legend=_legend_for(_present_colors(cells, drawable_routes)),
        )
    )
