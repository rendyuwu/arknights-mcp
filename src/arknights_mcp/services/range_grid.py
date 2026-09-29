"""Attack-range grid encoding + the unresolved-id disclosure.

``range_id`` ("3-1", "x-4") rides every operator phase and skill level. This module is
the single home for the two things the wire then owes it: laying a resolved grid
out as a compact, readable board, and phrasing the limitation for an id this build
cannot resolve.

The board encoding follows :mod:`~arknights_mcp.services.stage_tile_grid`:
one short string per grid row plus a fixed symbol alphabet, so a range reads at a
glance instead of as a coordinate soup. Unlike the stage grid, the raw coordinates are
kept ALONGSIDE the rows -- the stage grid dropped its per-tile objects under measured
size pressure (117 objects, three page round-trips), while a range is ~11 cells and a
mean of 2 distinct grids per operator response, so dropping the typed coordinates would
buy nothing and force a client to parse ASCII art to get a machine-usable answer -- a
forced second step the emission rule objects to.

The frame ALWAYS contains the deploy tile at ``(0, 0)`` even when the range does not
cover it (7 of 68 EN grids at the pinned commit do not), and the origin gets its own
two symbols -- covered and uncovered -- so the reader can locate the operator on the
board. Collapsing the uncovered origin into the plain "not covered" symbol would make
the frame ambiguous and the encoding lossy.

Pure transform: no SQL, no network, and no imported prose reaches it -- a grid
is integers only.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

#: The board alphabet. These are the SERVER's own symbols, not a
#: source enum, so they ship with the payload that uses them rather than as a
#: value domain. ``origin_uncovered`` is a distinct character on purpose: a range that
#: does not cover the deploy tile must stay distinguishable from one that does.
RANGE_GRID_SYMBOLS: dict[str, str] = {
    "origin_covered": "@",
    "origin_uncovered": "o",
    "covered": "#",
    "absent": ".",
}

#: Fail-closed ceiling on the laid-out frame. Real grids span rows -3..3 and
#: cols -3..6 in both regions at the pinned commit (a 7x10 frame, 70 cells), so this is
#: ~14x headroom and never bites; a pathological upstream grid is refused rather than
#: expanded into a huge rows block.
MAX_RANGE_GRID_CELLS = 1024


def range_grid_rows(grids: Sequence[tuple[int, int]]) -> tuple[str, ...]:
    """Lay ``grids`` out as one string per row, top row first.

    The frame is the bounding box of the covered cells UNIONED with the origin, so the
    deploy tile is always on the board and every row string is positionally comparable.
    Rows ascend by ``row`` index, matching the stage tile grid's top-row-first
    convention so two boards in one response read the same way up.

    Returns ``()`` when there is nothing to lay out, or when the frame exceeds
    :data:`MAX_RANGE_GRID_CELLS` (fail-closed); the caller then emits the typed
    coordinates alone rather than a truncated board.
    """
    if not grids:
        return ()
    covered = set(grids)
    rows = [r for r, _ in grids]
    cols = [c for _, c in grids]
    # Union with the origin: the deploy tile anchors the frame even when uncovered.
    min_row, max_row = min(*rows, 0), max(*rows, 0)
    min_col, max_col = min(*cols, 0), max(*cols, 0)
    if (max_row - min_row + 1) * (max_col - min_col + 1) > MAX_RANGE_GRID_CELLS:
        return ()
    out: list[str] = []
    for row in range(min_row, max_row + 1):
        chars: list[str] = []
        for col in range(min_col, max_col + 1):
            is_origin = row == 0 and col == 0
            if (row, col) in covered:
                chars.append(
                    RANGE_GRID_SYMBOLS["origin_covered"]
                    if is_origin
                    else RANGE_GRID_SYMBOLS["covered"]
                )
            else:
                chars.append(
                    RANGE_GRID_SYMBOLS["origin_uncovered"]
                    if is_origin
                    else RANGE_GRID_SYMBOLS["absent"]
                )
        out.append("".join(chars))
    return tuple(out)


def unresolved_range_limitation(unresolved: Iterable[str], *, max_named: int = 8) -> str | None:
    """Caption for emitted ``range_id`` values this build cannot resolve.

    An emitted opaque id allows two outcomes: pair it with the resolution, or
    emit the id plus a limitation saying the resolution is unavailable. This is the
    second arm, and it is the whole response on a build whose snapshot carried no
    ``range_table.json`` (optional per snapshot) or that predates migration
    0020. Never a fabricated grid.

    Bounded like every other id list on the wire: at most ``max_named`` ids are named
    and the remainder is stated as an EXACT count, so the caption cannot grow without
    limit yet never under-reports. Returns ``None`` when
    everything resolved -- an empty limitation would be noise.
    """
    ids = sorted(set(unresolved))
    if not ids:
        return None
    named = ", ".join(ids[:max_named])
    remainder = len(ids) - max_named
    tail = f", and {remainder} more" if remainder > 0 else ""
    return (
        f"attack-range grid unavailable for range_id {named}{tail}: this build's snapshot "
        "carried no range_table entry for it, so the id is emitted unresolved rather than "
        "guessed. Ask the server admin to run `arknights-mcp sync` against a snapshot "
        "that includes gamedata/excel/range_table.json to resolve it."
    )
