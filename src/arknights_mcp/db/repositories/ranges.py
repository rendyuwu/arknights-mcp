"""Read-only repository for the attack-range grid dimension (§T200; §V2/§V69/B132).

Resolves the ``range_id`` an operator phase or skill level emits into the grid of
deploy-tile-relative cells imported from ``range_table.json``. One batched lookup per
response, region-scoped so an ``en`` operator's range never resolves through a ``cn``
grid (§V5).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass

from arknights_mcp.db.repositories.base import Repository

#: One batched read. Only the placeholder COUNT is composed from ``len(ids)``; every
#: id is bound through a ``?`` so injection stays impossible (§V2). Served by the
#: ``UNIQUE(server, range_id)`` implicit index, whose leading column this filters
#: (§V94/B115).
_RANGES_BY_IDS_SQL_PREFIX = (
    "SELECT range_id, grids_json FROM ranges WHERE server = ? AND range_id IN "
)


@dataclass(frozen=True)
class RangeRow:
    """One resolved attack-range grid: ordered ``(row, col)`` offsets from the deploy tile."""

    range_id: str
    grids: tuple[tuple[int, int], ...]


def _decode_grids(raw: str) -> tuple[tuple[int, int], ...]:
    """Decode the stored compact ``[[row, col], ...]`` array back to typed pairs.

    A malformed or non-pair entry is dropped rather than guessed at: a fabricated
    coordinate would claim coverage the source never stated (§V26). The importer only
    ever writes integer pairs, so this is a decode floor, not an expected path.
    """
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(decoded, list):
        return ()
    cells: list[tuple[int, int]] = []
    for cell in decoded:
        if (
            isinstance(cell, list)
            and len(cell) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in cell)
        ):
            cells.append((cell[0], cell[1]))
    return tuple(cells)


class RangeRepository(Repository):
    """Batched ``range_id`` -> grid resolution for the operator service (§V69)."""

    def by_ids(self, server: str, range_ids: Collection[str]) -> dict[str, RangeRow]:
        """Map each resolvable ``range_id`` to its grid for this build and region.

        A single batched ``WHERE range_id IN (?, …)`` rather than per-id round-trips,
        mirroring :meth:`~arknights_mcp.db.repositories.operators.OperatorRepository
        .item_display_names` (§V37: the same §V69 resolve-or-disclose shape, so the two
        read the same way). Only the placeholder count is composed from ``len(ids)``;
        no id is interpolated into the SQL (§V2). An empty id set short-circuits with no
        query.

        An id ABSENT from the returned map had no grid in this build, so the caller
        emits the id as-is plus a limitation and never fabricates a grid (§V26/§V69).
        Degrades on table absence for the same reason
        :meth:`~arknights_mcp.db.repositories.operators.OperatorRepository.skins` does:
        an active database built before migration 0020 has no ``ranges`` table, and this
        read must return an empty map (every id then takes the limitation arm, §V21
        backward compatibility) rather than surface ``no such table`` as an
        ``internal_error`` on every ``get_operator`` call. The degrade is a catch on the
        query itself, not a per-call ``sqlite_master`` probe -- the common
        (table-present) path pays zero extra queries.
        """
        ids = sorted({r for r in range_ids if r})
        if not ids:
            return {}
        placeholders = ", ".join("?" * len(ids))
        sql = f"{_RANGES_BY_IDS_SQL_PREFIX}({placeholders})"
        try:
            rows = self._all(sql, (server, *ids))
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return {}
            raise
        resolved: dict[str, RangeRow] = {}
        for range_id, grids_json in rows:
            grids = _decode_grids(grids_json)
            if not grids:
                # A row that decodes to no cell resolves to nothing: reporting it as
                # resolved would ship an empty grid as a "covers nothing" fact (§V67).
                continue
            resolved[range_id] = RangeRow(range_id=range_id, grids=grids)
        return resolved
