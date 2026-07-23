"""Compact per-row encoding of a stage tile grid (§V74 (c)/§V66/§V22).

A stage grid used to ride the wire as one object per tile (13x9 = 117 objects,
~3 page round-trips). This module lays the board out as one short string per grid
row plus a small symbol legend, so a whole board fits one response instead of
paging. It is a self-contained, pure transform over the typed tile rows --
no SQL, no network, no imported prose reaches it (§V18) -- kept out of the stage
service module so that module stays within the §V38 size cap (parallel to the
render-own map image in :mod:`~arknights_mcp.services.stage_map_render`).

Both transports reach this through the shared stage service (§V14); it holds the
single home for the grid encoding (§V37).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from arknights_mcp.db.repositories.stages import StageTileRow
from arknights_mcp.services.stage_map_render import MAX_MAP_CELLS

#: §V74 (c): the character a grid cell shows when the source stored no tile there.
_GRID_ABSENT_SYMBOL = "."
#: §V74 (c): the symbol pool for distinct tile types, assigned in first-appearance
#: order (scanning top row down, left to right). ``.`` is reserved for absent cells
#: and excluded here. 62 symbols is far beyond the handful of distinct tile types a
#: real stage grid carries; a board exceeding it is refused rather than encoded
#: ambiguously (fail-closed).
_GRID_SYMBOLS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

#: The four typed tile fields whose distinct combinations get a legend symbol.
_TileTypeKey = tuple[str | None, str | None, str | None, bool | None]


@dataclass(frozen=True)
class TileLegendEntry:
    """One distinct tile type in the compact grid legend (§V74 (c)).

    ``symbol`` is the single character the tile occupies in every ``rows`` string;
    the remaining fields are the tile's typed, already-allowlisted values (§V18) --
    the same four the per-tile object carried before the grid was compacted.
    """

    symbol: str
    tile_key: str | None
    height_type: str | None
    buildable_type: str | None
    passable: bool | None


@dataclass(frozen=True)
class TileGridFacts:
    """Compact per-row string encoding of the stage tile grid (§V74 (c)/§V66/§V22).

    A stage grid was emitted as one object per tile (13x9 = 117 objects, ~3 page
    round-trips). The board is instead laid out as ``rows`` -- one string per grid
    row, the top row (highest ``y``) first so it reads like the rendered map --
    where each character is a ``legend`` symbol (``absent_symbol`` where the source
    stored no tile for that cell). ``legend`` maps each symbol back to its typed
    tile fields. A whole board is now a handful of short strings plus a small
    legend, so it rides one response instead of paging (§V74 (c)).
    """

    rows: tuple[str, ...]
    legend: tuple[TileLegendEntry, ...]
    absent_symbol: str


def tile_grid_oversize_limitation() -> str:
    """§V22 caption for a board with more raw tiles than the cap (single §V37 home)."""
    return (
        f"tile grid omitted: the stage has more than {MAX_MAP_CELLS} tiles; "
        "use include_map_image for a rendered overview instead"
    )


def tile_grid_refused_limitation() -> str:
    """§V26/§V22 caption when a non-empty board cannot be laid out compactly.

    Distinct from :func:`tile_grid_oversize_limitation` (raw tile COUNT over the cap):
    a board can carry few tiles yet a huge extent (a sparse-wide board over the
    extent-product cap, §V74 (c)/B70) or more distinct tile types than the symbol pool.
    Emitting it means a refused grid is never a silent ``None`` a client reads as
    "board has no tiles" (§V26/B71). Single §V37 home."""
    return (
        "tile grid omitted: the board extent or distinct-tile-type count exceeds the "
        "compact-encoding limit; use include_map_image for a rendered overview instead"
    )


def build_tile_grid(
    tiles: Sequence[StageTileRow], width: int | None, height: int | None
) -> TileGridFacts | None:
    """Encode the stage tile rows into the compact per-row grid (§V74 (c)/§V66).

    Assigns each DISTINCT ``(tile_key, height_type, buildable_type, passable)``
    tuple a stable legend symbol in first-appearance order (top row down, left to
    right), then lays every grid position out as one character -- the tile's symbol,
    or ``_GRID_ABSENT_SYMBOL`` where the source stored no tile. A full board becomes
    a handful of short strings plus a small legend instead of N tile objects, so it
    rides one response rather than three pages. Rows are emitted top row (highest
    ``y``) first so the text grid reads the same way up as the rendered image
    (:func:`~arknights_mcp.services.stage_map_render.render_stage_map`).

    Returns ``None`` when there is nothing to lay out (no tiles / no extent), when the
    board extent exceeds :data:`MAX_MAP_CELLS` cells (extent-product guard, parity with
    the render, §V74 (c)/B70), or when the distinct-type count exceeds the symbol pool
    (a pathological board refused rather than encoded ambiguously). Use
    :func:`resolve_tile_grid` when a refusal must carry a §V26 limitation. No imported
    prose reaches the grid -- only the typed, already-allowlisted tile fields (§V18)."""
    if not tiles:
        return None
    # §V74 (c)/B72: the effective extent must COVER every tile -- a stored width/height
    # that UNDER-reports the real tile extent (a tile at x=15 with a stored width of 13)
    # would otherwise never be visited by the range() below and be silently dropped, so
    # take max(stored_dim, max coord + 1).
    eff_w = max(width if (width is not None and width > 0) else 0, max(t.x for t in tiles) + 1)
    eff_h = max(height if (height is not None and height > 0) else 0, max(t.y for t in tiles) + 1)
    if eff_w <= 0 or eff_h <= 0:
        return None
    # §V74 (c)/B70: extent-product guard, parity with render_stage_map -- a sparse board
    # (2 tiles at (0,0) and (2000,2000)) passes a row-COUNT cap yet spans a 2001x2001
    # grid (~4M-char rows string = §V22 breach), so refuse on extent, not just count.
    if eff_w * eff_h > MAX_MAP_CELLS:
        return None
    by_xy: dict[tuple[int, int], StageTileRow] = {(t.x, t.y): t for t in tiles}
    symbols: dict[_TileTypeKey, str] = {}
    legend: list[TileLegendEntry] = []
    rows: list[str] = []
    for y in range(eff_h - 1, -1, -1):
        chars: list[str] = []
        for x in range(eff_w):
            tile = by_xy.get((x, y))
            if tile is None:
                chars.append(_GRID_ABSENT_SYMBOL)
                continue
            key: _TileTypeKey = (
                tile.tile_key,
                tile.height_type,
                tile.buildable_type,
                tile.passable,
            )
            sym = symbols.get(key)
            if sym is None:
                if len(legend) >= len(_GRID_SYMBOLS):
                    return None  # more distinct tile types than symbols -- refuse
                sym = _GRID_SYMBOLS[len(legend)]
                symbols[key] = sym
                legend.append(
                    TileLegendEntry(
                        symbol=sym,
                        tile_key=tile.tile_key,
                        height_type=tile.height_type,
                        buildable_type=tile.buildable_type,
                        passable=tile.passable,
                    )
                )
            chars.append(sym)
        rows.append("".join(chars))
    return TileGridFacts(rows=tuple(rows), legend=tuple(legend), absent_symbol=_GRID_ABSENT_SYMBOL)


def resolve_tile_grid(
    tiles: Sequence[StageTileRow], width: int | None, height: int | None
) -> tuple[TileGridFacts | None, str | None]:
    """Encode the grid, pairing a refused non-empty board with its §V26 limitation.

    Single §V37 home for the whole tile-grid decision so the stage service only wires
    the result. Returns:

    * ``(grid, None)`` -- laid out in budget;
    * ``(None, None)`` -- there are simply no tiles, so the caller emits no grid and no
      limitation (an absent grid is honest here);
    * ``(None, limitation)`` -- the raw tile COUNT is over the cap
      (:func:`tile_grid_oversize_limitation`), or a non-empty board was REFUSED by
      :func:`build_tile_grid` (extent over the cap / too many distinct types / degenerate
      extent → :func:`tile_grid_refused_limitation`) so a refused grid is never a silent
      ``None`` a client reads as "no tiles" (§V26/§V22/B70/B71)."""
    if len(tiles) > MAX_MAP_CELLS:
        return None, tile_grid_oversize_limitation()
    grid = build_tile_grid(tiles, width, height)
    if grid is None and tiles:
        return None, tile_grid_refused_limitation()
    return grid, None
