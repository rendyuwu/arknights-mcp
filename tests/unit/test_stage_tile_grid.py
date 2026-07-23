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
    tile_grid_oversize_limitation,
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
