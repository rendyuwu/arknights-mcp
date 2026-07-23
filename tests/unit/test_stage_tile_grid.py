"""Unit tests for the compact tile-grid encoder (§V74 (c)/§V66/§V22, §T145).

Exercise :func:`~arknights_mcp.services.stage_tile_grid.build_tile_grid` directly
(the tool-level path is covered against the pinned 4-4 fixture in
``test_get_stage_tool``): per-row string layout top-row-first, symbol reuse for
identical tile types, the reserved absent symbol, extent derivation, and the
fail-closed refusal when a pathological board has more distinct tile types than
the symbol pool.
"""

from __future__ import annotations

from arknights_mcp.db.repositories.stages import StageTileRow
from arknights_mcp.services.stage_map_render import MAX_MAP_CELLS
from arknights_mcp.services.stage_tile_grid import (
    _GRID_SYMBOLS,
    build_tile_grid,
    resolve_tile_grid,
    tile_grid_oversize_limitation,
    tile_grid_refused_limitation,
)


def _tile(x: int, y: int, key: str, passable: bool = True) -> StageTileRow:
    return StageTileRow(
        x=x, y=y, tile_key=key, height_type="LOWLAND", buildable_type="NONE", passable=passable
    )


def test_build_tile_grid_encodes_rows_top_first_with_reused_symbols() -> None:
    # §V74 (c): rows are laid out top row (highest y) first; two tiles of the SAME
    # (tile_key, height_type, buildable_type, passable) type share ONE legend symbol.
    tiles = [
        _tile(0, 0, "tile_road"),
        _tile(1, 0, "tile_road"),
        StageTileRow(
            x=2,
            y=1,
            tile_key="tile_wall",
            height_type="HIGHLAND",
            buildable_type="RANGED",
            passable=False,
        ),
    ]
    grid = build_tile_grid(tiles, width=3, height=2)
    assert grid is not None
    assert grid.absent_symbol == "."
    # Top row (y=1) carries only the wall at x=2; bottom row (y=0) the two roads.
    assert len(grid.rows) == 2 and all(len(r) == 3 for r in grid.rows)
    assert grid.rows[0][0] == "." and grid.rows[0][1] == "."  # absent cells
    # The two road cells reuse one symbol (identical tile type).
    assert grid.rows[1][0] == grid.rows[1][1]
    assert grid.rows[1][2] == "."
    by_symbol = {e.symbol: e for e in grid.legend}
    assert by_symbol[grid.rows[1][0]].tile_key == "tile_road"
    assert by_symbol[grid.rows[0][2]].tile_key == "tile_wall"
    # Two distinct types -> two legend entries (road reused, not duplicated).
    assert len(grid.legend) == 2


def test_build_tile_grid_none_when_no_tiles() -> None:
    # Nothing to lay out -> None (the caller then emits no tile_grid).
    assert build_tile_grid([], width=3, height=3) is None


def test_build_tile_grid_derives_extent_without_dimensions() -> None:
    # §V74 (c): with no stored width/height, the extent is derived from the max tile
    # coordinate so a grid with only tiles still encodes.
    grid = build_tile_grid([_tile(0, 0, "a"), _tile(2, 1, "b")], width=None, height=None)
    assert grid is not None
    assert len(grid.rows) == 2 and all(len(r) == 3 for r in grid.rows)


def test_build_tile_grid_refuses_more_types_than_symbol_pool() -> None:
    # §V74 (c) fail-closed: a board with more distinct tile types than the symbol
    # pool is refused (None) rather than encoded ambiguously.
    over = len(_GRID_SYMBOLS) + 1
    tiles = [_tile(i, 0, f"tile_{i}") for i in range(over)]
    assert build_tile_grid(tiles, width=over, height=1) is None


def test_oversize_limitation_is_client_facing() -> None:
    # §V22 caption names the cap and stays client-facing (§V71: no spec cites).
    text = tile_grid_oversize_limitation()
    assert "tile grid omitted" in text
    assert str(MAX_MAP_CELLS) in text
    assert "§" not in text and "V74" not in text


def test_build_tile_grid_refuses_sparse_wide_over_extent() -> None:
    # §V74 (c)/§V22/B70: a sparse board with only TWO tiles but a 2001x2001 extent
    # passes any row-COUNT cap (len == 2) yet would build a ~4M-char rows string. The
    # extent-product guard (parity with the render) refuses it rather than breach §V22.
    far = 2_000  # (far + 1) ** 2 == 2001*2001 ~ 4M > MAX_MAP_CELLS
    tiles = [_tile(0, 0, "a"), _tile(far, far, "b")]
    assert len(tiles) <= MAX_MAP_CELLS  # count gate alone would let this through
    assert build_tile_grid(tiles, width=None, height=None) is None
    # A stored width/height that reports the huge extent is refused just the same.
    assert build_tile_grid(tiles, width=far + 1, height=far + 1) is None


def test_build_tile_grid_extent_covers_tiles_beyond_stored_width() -> None:
    # §V74 (c)/B72: a stored width/height that UNDER-reports the real tile extent
    # (tile at x=15 with a stored width of 13) must NOT silently drop the out-of-range
    # tile -- the effective extent is max(stored_dim, max coord + 1).
    tiles = [_tile(0, 0, "road"), _tile(15, 0, "wall")]
    grid = build_tile_grid(tiles, width=13, height=1)
    assert grid is not None
    assert len(grid.rows) == 1 and len(grid.rows[0]) == 16  # covers x=15, not clipped to 13
    by_symbol = {e.symbol: e for e in grid.legend}
    assert by_symbol[grid.rows[0][15]].tile_key == "wall"  # the x=15 tile survived
    assert {e.tile_key for e in grid.legend} == {"road", "wall"}


# --- resolve_tile_grid: the single-home decision (grid + say-so limitation) ---------


def test_resolve_tile_grid_ok_carries_grid_no_limitation() -> None:
    grid, limitation = resolve_tile_grid([_tile(0, 0, "a"), _tile(1, 0, "b")], width=2, height=1)
    assert grid is not None and limitation is None


def test_resolve_tile_grid_no_tiles_is_silent() -> None:
    # §V26: an absent grid is honest when there are simply no tiles -> no limitation.
    grid, limitation = resolve_tile_grid([], width=3, height=3)
    assert grid is None and limitation is None


def test_resolve_tile_grid_refused_board_carries_limitation() -> None:
    # §V26/B71: a NON-empty board that build refuses (here over-extent, B70) is never a
    # silent None -- it pairs with a say-so limitation distinguishable from "no tiles".
    far = 2_000
    grid, limitation = resolve_tile_grid([_tile(0, 0, "a"), _tile(far, far, "b")], None, None)
    assert grid is None
    assert limitation == tile_grid_refused_limitation()


def test_resolve_tile_grid_over_count_cap_uses_oversize_limitation() -> None:
    # A raw tile COUNT over the cap keeps the count-specific caption (not the extent one).
    tiles = [_tile(i % 100, i // 100, "a") for i in range(MAX_MAP_CELLS + 1)]
    grid, limitation = resolve_tile_grid(tiles, width=None, height=None)
    assert grid is None
    assert limitation == tile_grid_oversize_limitation()


def test_refused_limitation_is_client_facing() -> None:
    # §V26/§V22 caption stays client-facing (§V71: no spec cites) and distinguishes a
    # refused board from an absent one.
    text = tile_grid_refused_limitation()
    assert "tile grid omitted" in text
    assert "§" not in text and "V74" not in text and "V26" not in text
    assert text != tile_grid_oversize_limitation()
