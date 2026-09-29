"""Attack-range grid importer: range_table.json -> ranges.

Parses the primary ``range_table.json`` -- the SAME ``arknights_assets_gamedata``
snapshot as enemy/stage/operator, NOT a new source -- into the ``ranges`` dimension
table that resolves the ``range_id`` every operator phase and skill level emits.

The file was DECLARED in that source's registry ``fields_consumed`` while no importer
read it and no sync fetched it, so the declaration promised a resolution that did not
exist and ``range_id`` shipped bare with neither a resolver nor a limitation. This
module is the resolver arm.

The shape is an id-keyed dict of ``{id, direction, grids: [{row, col}]}`` (verified at
the pinned commit 413a81a3ff3e: 68 EN / 73 CN entries, every entry's ``id`` equal to
its key). Only ``id`` + ``grids`` are allowlisted -- ``direction`` is the
constant ``1`` everywhere at the pin with unverified semantics and no reader, a dead
column the schema forbids; the live-upstream guard pins that constancy instead.

Pure parsing (:func:`parse_ranges`) is separated from the DB write so it is unit
testable without a database. An entry with no id or no usable grid cell is skipped
fail-closed -- an empty grid would read on the wire as "this range covers nothing",
a claim the source did not make. A snapshot without ``range_table.json`` (a
combat-only fixture) yields an empty result rather than failing: the table is fetched
tolerant-absent and ``ranges`` is not a CRITICAL_TABLE, so the wire then takes the
limitation arm. A non-empty source that resolves to zero rows still fails
closed so a shape mismatch is never promoted as a silently empty domain.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.field_policy import (
    RANGE_ALLOWLIST,
    RANGE_GRID_ALLOWLIST,
    apply_allowlist,
)
from arknights_mcp.importers.guards import guard_not_silently_empty
from arknights_mcp.importers.manifest import insert_record_provenance
from arknights_mcp.sources.base import SourceAdapter
from arknights_mcp.util.coerce import as_dict, as_int, as_str
from arknights_mcp.util.sqlite import integrity_guard

#: Default snapshot path (an introspection test reads this signature default).
RANGE_TABLE_PATH = "gamedata/excel/range_table.json"


@dataclass(frozen=True)
class ParsedRange:
    """One attack-range grid: its id plus the deploy-tile-relative covered cells."""

    range_id: str
    grids: tuple[tuple[int, int], ...]
    provenance_record: dict[str, Any]


@dataclass(frozen=True)
class RangeImportResult:
    """Per-server outcome of the range import."""

    ranges_inserted: int = 0


def _grid_cells(raw_grids: Any) -> tuple[tuple[int, int], ...]:
    """Allowlist a raw ``grids`` list to ordered ``(row, col)`` integer pairs.

    A cell missing either coordinate is dropped rather than defaulted to 0: a
    fabricated origin cell would claim coverage the source never stated.
    Cells are de-duplicated and sorted so the stored JSON is deterministic across
    builds (two snapshots with the same grid hash the same).
    """
    if not isinstance(raw_grids, list):
        return ()
    cells: set[tuple[int, int]] = set()
    for entry in raw_grids:
        kept = apply_allowlist(as_dict(entry), RANGE_GRID_ALLOWLIST).kept
        row = as_int(kept.get("row"))
        col = as_int(kept.get("col"))
        if row is None or col is None:
            continue
        cells.add((row, col))
    return tuple(sorted(cells))


def parse_ranges(range_raw: Any) -> list[ParsedRange]:
    """Transform a raw ``range_table`` into typed, allowlisted ranges.

    Iteration is key-sorted so parse order (and thus provenance ids) is deterministic
    across builds. The entry's own ``id`` is preferred over its dict key and the key is
    the fallback -- they are equal on all 68 EN / 73 CN entries at the pin, so this only
    matters if upstream ever diverges, and the id a phase/skill row references is the
    key. An entry that yields no usable cell is skipped (fail-closed): an empty grid
    would ship as a positive "covers nothing" claim.
    """
    if not isinstance(range_raw, dict):
        raise ImporterError("range_table is not a JSON object")
    out: list[ParsedRange] = []
    for key in sorted(range_raw, key=str):
        kept = apply_allowlist(as_dict(range_raw[key]), RANGE_ALLOWLIST).kept
        range_id = as_str(kept.get("id")) or as_str(key)
        if not range_id:
            continue
        cells = _grid_cells(kept.get("grids"))
        if not cells:
            continue
        out.append(
            ParsedRange(
                range_id=range_id,
                grids=cells,
                provenance_record=kept,
            )
        )
    return out


def insert_ranges(
    conn: sqlite3.Connection,
    parsed: list[ParsedRange],
    *,
    server: str,
    snapshot_id: str,
    source_path: str,
) -> RangeImportResult:
    """Insert ``ranges`` rows with per-record provenance.

    Each grid is stored as a compact ``[[row, col], ...]`` JSON array -- pairs rather
    than ``{"row": .., "col": ..}`` objects, which halves the stored bytes for a value
    read only through :class:`~arknights_mcp.db.repositories.ranges.RangeRepository`,
    which restores the named shape. A duplicate ``range_id`` collides on
    ``UNIQUE(server, range_id)``; that anomaly maps to a typed :class:`ImporterError`
    rather than an uncaught ``IntegrityError`` tearing down the multi-region build.
    """
    inserted = 0
    for entry in parsed:
        provenance_id = insert_record_provenance(
            conn,
            snapshot_id=snapshot_id,
            source_path=source_path,
            source_record_key=entry.range_id,
            record=entry.provenance_record,
        )
        with integrity_guard(
            f"range {entry.range_id!r} collides on a UNIQUE constraint (duplicate range id)",
            ImporterError,
        ):
            conn.execute(
                "INSERT INTO ranges (server, range_id, grids_json, provenance_id) "
                "VALUES (?, ?, ?, ?)",
                (
                    server,
                    entry.range_id,
                    json.dumps([[r, c] for r, c in entry.grids], separators=(",", ":")),
                    provenance_id,
                ),
            )
        inserted += 1
    return RangeImportResult(ranges_inserted=inserted)


def import_ranges(
    conn: sqlite3.Connection,
    adapter: SourceAdapter,
    snapshot_id: str,
    *,
    range_table_path: str = RANGE_TABLE_PATH,
) -> RangeImportResult:
    """Read ``range_table.json`` via the adapter and import the attack-range grids.

    A snapshot without the file yields an empty result rather than failing, so the
    range domain is optional per snapshot and the wire falls back to the
    limitation arm. A non-empty source table that produces zero rows fails closed
    so a shape mismatch is never promoted as a silently empty domain -- the
    candidate is discarded and the active DB stays untouched.
    """
    if not adapter.exists(range_table_path):
        return RangeImportResult()
    range_raw = adapter.read_json(range_table_path)
    parsed = parse_ranges(range_raw)
    guard_not_silently_empty(
        candidates=len(range_raw) if isinstance(range_raw, dict) else 0,
        produced=len(parsed),
        scope=adapter.server,
        source="range_table",
        unit="attack-range entr(y|ies)",
        resolution="parsed to a range grid",
        outcome="empty range domain",
    )
    return insert_ranges(
        conn,
        parsed,
        server=adapter.server,
        snapshot_id=snapshot_id,
        source_path=range_table_path,
    )
