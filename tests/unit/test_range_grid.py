"""T200: the range board encoding + §V69's unresolved-id arm (§V74 (c)/§V22/§V26; B132).

Every grid here is TRANSCRIBED from the real ``range_table.json`` at the pinned commit
(B107/§V29). The board is what makes a resolved ``range_id`` readable rather than a
coordinate soup, so the cases that matter are the ones where a naive bounding box would
lie: a grid that does not cover its own deploy tile, and one that reaches only to one
side of it.
"""

from __future__ import annotations

from arknights_mcp.services.range_grid import (
    MAX_RANGE_GRID_CELLS,
    RANGE_GRID_SYMBOLS,
    range_grid_rows,
    unresolved_range_limitation,
)

# Transcribed from upstream at 413a81a3ff3e (EN).
REAL_1_1 = ((0, 0), (0, 1))
REAL_1_2 = ((-1, 0), (0, 0), (0, 1), (1, 0))
REAL_2_7 = ((0, 2),)  # ORIGIN-UNCOVERED: 7 of 68 EN grids are
REAL_X_1 = (
    (-2, 0),
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, -2),
    (0, -1),
    (0, 0),
    (0, 1),
    (0, 2),
    (1, -1),
    (1, 0),
    (1, 1),
    (2, 0),
)


def test_smallest_real_grid() -> None:
    assert range_grid_rows(REAL_1_1) == ("@#",)


def test_cross_grid_reads_as_the_board() -> None:
    assert range_grid_rows(REAL_1_2) == ("#.", "@#", "#.")


def test_diamond_grid_reads_as_the_board() -> None:
    """x-1 is Amiya's Chain Cast range; the rendered board is a diamond, as in game."""
    assert range_grid_rows(REAL_X_1) == (
        "..#..",
        ".###.",
        "##@##",
        ".###.",
        "..#..",
    )


def test_origin_is_on_the_board_even_when_uncovered() -> None:
    """The frame unions the deploy tile, and the uncovered origin gets its OWN symbol.

    A bounding box over the covered cells alone would render 2-7 as ``"#"`` -- identical
    to a range that covers exactly the deploy tile, which is the opposite claim. Folding
    the uncovered origin into the plain absent symbol would render ``"..#"`` and lose
    where the operator stands, making the encoding lossy.
    """
    assert range_grid_rows(REAL_2_7) == ("o.#",)
    assert range_grid_rows(((0, 0),)) == ("@",)


def test_symbols_are_four_distinct_characters() -> None:
    """Two symbols colliding would silently merge two different claims."""
    assert len(set(RANGE_GRID_SYMBOLS.values())) == len(RANGE_GRID_SYMBOLS) == 4


def test_empty_grid_lays_out_nothing() -> None:
    assert range_grid_rows(()) == ()


def test_pathological_extent_is_refused_not_expanded() -> None:
    """§V22 fail-closed: two far-apart cells must not become a giant rows block."""
    assert range_grid_rows(((0, 0), (0, MAX_RANGE_GRID_CELLS))) == ()
    # ...while every real grid stays far inside the ceiling.
    assert range_grid_rows(REAL_X_1)


# --- §V69's other arm ----------------------------------------------------------


def test_no_unresolved_ids_means_no_limitation() -> None:
    """An empty caption would be noise on the response that resolved everything."""
    assert unresolved_range_limitation(()) is None


def test_unresolved_ids_are_named() -> None:
    note = unresolved_range_limitation(("x-1", "1-1"))
    assert note is not None
    # Sorted + deduplicated so the caption is stable across responses.
    assert "range_id 1-1, x-1" in note
    # §V26: it says the build lacks the entry, never that the range does not exist.
    assert "guessed" in note
    # §V28/§V71 (a): a limitation names the admin action that would fix it.
    assert "arknights-mcp sync" in note


def test_unresolved_list_is_bounded_with_an_exact_remainder() -> None:
    """§V22/§V66 (the T195 precedent): bounded, but never under-reporting."""
    note = unresolved_range_limitation(tuple(f"r-{i:02d}" for i in range(20)))
    assert note is not None
    assert "r-00" in note and "r-07" in note
    assert "r-08" not in note
    assert "and 12 more" in note


def test_duplicates_do_not_inflate_the_remainder_count() -> None:
    note = unresolved_range_limitation(("x-1", "x-1", "x-1"))
    assert note is not None
    assert "more" not in note
