"""range_table.json -> the `ranges` dimension.

Every grid asserted here is TRANSCRIBED from the real ``range_table.json`` at the
pinned upstream commit, never invented (a fixture that makes up the shape it
then asserts proves only that it is self-consistent). ``1-1``/``1-2``/``x-1`` are the
grids the operator fixture's own ``character_table``/``skill_table`` reference; ``2-7``
is a real ORIGIN-UNCOVERED grid, the case that makes the board frame load-bearing.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.ranges import (
    RANGE_TABLE_PATH,
    import_ranges,
    insert_ranges,
    parse_ranges,
)
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_EN = REPO_ROOT / "tests" / "fixtures" / "operator" / "en"
FIXTURE_CN = REPO_ROOT / "tests" / "fixtures" / "operator" / "cn"


def _real_table() -> dict[str, Any]:
    return json.loads((FIXTURE_EN / RANGE_TABLE_PATH).read_text(encoding="utf-8"))


def test_parses_the_real_shape() -> None:
    """The upstream entry is ``{id, direction, grids: [{row, col}]}``."""
    parsed = {r.range_id: r for r in parse_ranges(_real_table())}
    assert set(parsed) == {"1-1", "1-2", "2-7", "x-1"}
    # Transcribed from upstream: 1-1 is the deploy tile plus the tile in front of it.
    assert parsed["1-1"].grids == ((0, 0), (0, 1))
    # 2-7 does NOT contain the origin -- a real grid, and the reason the board frame
    # unions the deploy tile in rather than taking the bounding box of covered cells.
    assert parsed["2-7"].grids == ((0, 2),)
    assert len(parsed["x-1"].grids) == 13


def test_direction_is_dropped_by_the_allowlist() -> None:
    """An unread field must not become a stored column.

    The raw entries DO carry ``direction`` (this asserts the fixture is really the
    upstream shape, so the drop is exercised rather than vacuous), and no parsed record
    keeps it.
    """
    raw = _real_table()
    assert all("direction" in entry for entry in raw.values())
    for parsed in parse_ranges(raw):
        assert "direction" not in parsed.provenance_record
        assert set(parsed.provenance_record) <= {"id", "grids"}


def test_grid_cells_are_deduplicated_and_ordered() -> None:
    """Deterministic stored bytes: two snapshots with the same grid hash the same."""
    parsed = parse_ranges(
        {
            "t-1": {
                "id": "t-1",
                "direction": 1,
                "grids": [{"row": 1, "col": 0}, {"row": 0, "col": 0}, {"row": 1, "col": 0}],
            }
        }
    )
    assert parsed[0].grids == ((0, 0), (1, 0))


def test_cell_missing_a_coordinate_is_dropped_not_defaulted() -> None:
    """A fabricated (0, 0) would claim coverage the source never stated."""
    parsed = parse_ranges(
        {
            "t-1": {
                "id": "t-1",
                "grids": [{"row": 2, "col": 2}, {"row": 3}, {"col": 4}, "junk"],
            }
        }
    )
    assert parsed[0].grids == ((2, 2),)


def test_entry_with_no_usable_cell_is_skipped() -> None:
    """An empty grid would ship as a positive "covers nothing" claim."""
    assert parse_ranges({"t-1": {"id": "t-1", "grids": []}}) == []
    assert parse_ranges({"t-1": {"id": "t-1"}}) == []


def test_id_falls_back_to_the_dict_key() -> None:
    """The key is what a phase/skill row references, so it is never lost."""
    parsed = parse_ranges({"k-9": {"grids": [{"row": 0, "col": 0}]}})
    assert parsed[0].range_id == "k-9"


def test_non_object_table_is_a_typed_importer_error() -> None:
    with pytest.raises(ImporterError):
        parse_ranges([{"id": "1-1"}])


# --- the DB write --------------------------------------------------------------


_SOURCE_ID = "arknights_assets_gamedata"


def _candidate(tmp_path: Path) -> sqlite3.Connection:
    """A migrated candidate seeded with the primary source + one snapshot row."""
    from arknights_mcp.db.migrations import build_database

    conn = build_database(tmp_path / "cand.sqlite")
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) VALUES "
        "(?, 'gd', 'o', 'https://x/', 'game_data_repository', '[\"en\"]', '0', 'reviewed', "
        "'reviewed', 'derived', 'a', 1, '2026-07-21')",
        (_SOURCE_ID,),
    )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES "
        "('en:test', ?, 'en', '2026-07-30T00:00:00+00:00', 'h', 'active', 'test')",
        (_SOURCE_ID,),
    )
    return conn


def test_insert_stores_compact_pairs_with_provenance(tmp_path: Path) -> None:
    """Every row carries its provenance chain; the grid stores as [[r, c], ...]."""
    conn = _candidate(tmp_path)
    result = insert_ranges(
        conn,
        parse_ranges(_real_table()),
        server="en",
        snapshot_id="en:test",
        source_path=RANGE_TABLE_PATH,
    )
    assert result.ranges_inserted == 4
    row = conn.execute(
        "SELECT grids_json, provenance_id FROM ranges WHERE server = 'en' AND range_id = '1-1'"
    ).fetchone()
    assert json.loads(row[0]) == [[0, 0], [0, 1]]
    assert row[1] is not None


def test_duplicate_range_id_is_a_typed_error_not_an_integrity_error(tmp_path: Path) -> None:
    """An anomaly must not tear down the multi-region build uncaught."""
    conn = _candidate(tmp_path)
    parsed = parse_ranges({"1-1": {"id": "1-1", "grids": [{"row": 0, "col": 0}]}})
    kwargs = {"server": "en", "snapshot_id": "en:test", "source_path": RANGE_TABLE_PATH}
    insert_ranges(conn, parsed, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ImporterError, match="collides on a UNIQUE constraint"):
        insert_ranges(conn, parsed, **kwargs)  # type: ignore[arg-type]


def test_import_reads_the_snapshot_through_the_adapter(tmp_path: Path) -> None:
    conn = _candidate(tmp_path)
    adapter = LocalSnapshotAdapter(FIXTURE_EN, "en")
    assert import_ranges(conn, adapter, "en:test").ranges_inserted == 4
    assert conn.execute("SELECT count(*) FROM ranges").fetchone()[0] == 4


def test_absent_range_table_imports_zero_rather_than_failing(tmp_path: Path) -> None:
    """A combat-only snapshot legitimately lacks the file.

    The CN operator fixture ships no ``range_table.json``, so this is also the fixture
    that gives the limitation arm a real execution path -- the promoted build
    resolves everything, so a build-only test would leave that arm unrun.
    """
    conn = _candidate(tmp_path)
    adapter = LocalSnapshotAdapter(FIXTURE_CN, "cn")
    assert not adapter.exists(RANGE_TABLE_PATH)
    assert import_ranges(conn, adapter, "en:test").ranges_inserted == 0


def test_non_empty_source_yielding_zero_rows_fails_closed(tmp_path: Path) -> None:
    """A shape mismatch must never promote as a silently empty domain."""
    root = tmp_path / "snap"
    (root / "gamedata" / "excel").mkdir(parents=True)
    (root / RANGE_TABLE_PATH).write_text(
        json.dumps({"1-1": {"id": "1-1", "grids": []}}), encoding="utf-8"
    )
    with pytest.raises(ImporterError):
        import_ranges(_candidate(tmp_path), LocalSnapshotAdapter(root, "en"), "en:test")
