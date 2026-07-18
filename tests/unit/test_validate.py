"""T23: the candidate validation gate (§V4) + the ``validate`` CLI command.

Covers each gate check -- ``integrity_check``, ``foreign_key_check``,
critical-table presence, row-count sanity, cross-region orphan detection, the
FTS smoke test (skipped until §T31), and golden domain invariants -- plus the
CLI's exit code. A candidate is promotable only if every check passes (§V4).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from arknights_mcp.cli import main
from arknights_mcp.db.migrations import build_database, default_migrations_dir
from arknights_mcp.db.validate import validate_database
from arknights_mcp.importers.pipeline import ServerImport, build_candidate, seed_data_sources
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"


def _expected_schema() -> str:
    return sorted(default_migrations_dir().glob("[0-9]*.sql"))[-1].stem


def _valid_candidate(tmp_path: Path) -> Path:
    path = tmp_path / "cand.sqlite"
    build_candidate(
        path,
        [
            ServerImport(
                "en", LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot"), "local_snapshot"
            )
        ],
        registry=load_source_registry(REGISTRY),
    )
    return path


def _check(report, name: str):  # type: ignore[no-untyped-def]
    return next(c for c in report.checks if c.name == name)


# --- happy path ---------------------------------------------------------------


def test_valid_candidate_passes_all_checks(tmp_path: Path) -> None:
    report = validate_database(
        _valid_candidate(tmp_path), expected_schema_version=_expected_schema()
    )
    assert report.passed
    assert all(c.passed for c in report.checks)
    # §T31: the FTS index now exists and its smoke check queries it (no longer a
    # no-op pass).
    assert "FTS table(s) queryable" in _check(report, "fts_smoke").detail


# --- unreadable / corrupt files -----------------------------------------------


def test_missing_file_fails_closed(tmp_path: Path) -> None:
    report = validate_database(tmp_path / "does_not_exist.sqlite")
    assert not report.passed


def test_non_sqlite_file_fails_closed(tmp_path: Path) -> None:
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"this is definitely not a sqlite database" * 10)
    report = validate_database(junk)
    assert not report.passed


# --- integrity of the schema --------------------------------------------------


def test_missing_critical_table_detected(tmp_path: Path) -> None:
    path = _valid_candidate(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE stage_enemies")
    conn.commit()
    conn.close()
    report = validate_database(path)
    assert not report.passed
    assert not _check(report, "critical_tables").passed


def test_foreign_key_violation_detected(tmp_path: Path) -> None:
    path = _valid_candidate(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    # A record_provenance row pointing at a non-existent snapshot (dangling FK).
    conn.execute(
        "INSERT INTO record_provenance "
        "(snapshot_id, source_path, source_record_key, record_hash, transform_version, "
        "field_policy_version) VALUES ('ghost:snapshot', 'p', 'k', 'h', '1', '1')"
    )
    conn.commit()
    conn.close()
    report = validate_database(path)
    assert not report.passed
    assert not _check(report, "foreign_key_check").passed


# --- row counts ---------------------------------------------------------------


def test_row_counts_min_snapshots_knob(tmp_path: Path) -> None:
    """A seeded-but-empty build fails with the default gate, passes when the
    caller allows an empty rebuild (§V20 purge-to-empty)."""
    path = tmp_path / "empty.sqlite"
    conn = build_database(path)
    seed_data_sources(conn, load_source_registry(REGISTRY))
    conn.commit()
    conn.close()

    assert not validate_database(path, min_snapshots=1).passed
    empty_ok = validate_database(path, min_snapshots=0)
    assert empty_ok.passed
    assert _check(empty_ok, "row_counts").passed


# --- cross-region orphans (§V5) -----------------------------------------------


def test_cross_region_reference_detected(tmp_path: Path) -> None:
    path = _valid_candidate(tmp_path)
    conn = sqlite3.connect(path)
    # Flip one enemy to cn while an en stage still references it -> cross-region.
    conn.execute(
        "UPDATE enemies SET server = 'cn' WHERE enemy_pk = (SELECT MIN(enemy_pk) FROM enemies)"
    )
    conn.commit()
    conn.close()
    report = validate_database(path)
    assert not report.passed
    assert not _check(report, "orphans").passed


# --- golden invariants --------------------------------------------------------


def test_golden_bad_region_detected(tmp_path: Path) -> None:
    path = _valid_candidate(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE source_snapshots SET server = 'xx'")
    conn.commit()
    conn.close()
    report = validate_database(path)
    assert not report.passed
    assert not _check(report, "golden").passed


def test_golden_schema_version_mismatch_detected(tmp_path: Path) -> None:
    report = validate_database(_valid_candidate(tmp_path), expected_schema_version="9999_wrong")
    assert not report.passed
    assert not _check(report, "golden").passed


# --- CLI command --------------------------------------------------------------


def test_cli_validate_exit_codes(tmp_path: Path) -> None:
    good = _valid_candidate(tmp_path)
    assert main(["validate", "--database", str(good)]) == 0

    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"nope")
    assert main(["validate", "--database", str(bad)]) == 1
