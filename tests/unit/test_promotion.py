"""T24: versioned immutable builds + atomic ``current.json`` promotion.

Covers §V4 (promote only after validation; atomic swap via ``current.json``;
never mutate the active DB in place), the "unchanged -> no-op" rule, retain-N
pruning, and §V3 fail-closed behaviour (an unvalidated or malformed candidate
never replaces the current DB).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from arknights_mcp.analyzers.base import ANALYZER_VERSION
from arknights_mcp.db.migrations import build_database
from arknights_mcp.db.promotion import (
    BUILDS_DIRNAME,
    CURRENT_MANIFEST_NAME,
    CurrentManifest,
    PromotionError,
    promote_candidate,
    read_current_manifest,
    resolve_active_database,
)

Snapshot = tuple[str, str, str]  # (source_id, server, manifest_hash)

DEFAULT_SNAPSHOTS: list[Snapshot] = [("arknights_assets_gamedata", "en", "manifest-hash-A")]


def _make_candidate(path: Path, snapshots: list[Snapshot] | None = None) -> Path:
    """Build a migrated candidate DB seeded with the given source snapshots."""
    snaps = DEFAULT_SNAPSHOTS if snapshots is None else snapshots
    conn = build_database(path)
    for source_id, server, manifest_hash in snaps:
        conn.execute(
            "INSERT OR IGNORE INTO data_sources (source_id, display_name, owner_name, "
            "canonical_url, source_type, regions_json, adapter_version, license_status, "
            "permission_status, redistribution_status, attribution_text, enabled, "
            "last_reviewed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                source_id,
                "O",
                "https://x",
                "t",
                '["en"]',
                "1",
                "l",
                "p",
                "r",
                "a",
                1,
                "2026-07-17",
            ),
        )
        conn.execute(
            "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
            "manifest_hash, status, field_policy_version) VALUES (?,?,?,?,?,?,?)",
            (
                f"{server}:{manifest_hash[:12]}",
                source_id,
                server,
                "2026-07-17",
                manifest_hash,
                "imported",
                "1",
            ),
        )
    conn.commit()
    conn.close()
    return path


def _ts(hour: int) -> datetime:
    return datetime(2026, 7, 17, hour, 0, 0, tzinfo=UTC)


# --- §V4: atomic, validated promotion -------------------------------------


def test_promote_writes_versioned_build_and_manifest(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    candidate = _make_candidate(tmp_path / "candidate.sqlite")

    result = promote_candidate(
        candidate, data_dir=data_dir, validation_passed=True, timestamp=_ts(10)
    )

    assert result.status == "promoted"
    build = data_dir / BUILDS_DIRNAME / result.manifest.database_filename
    assert build.is_file()
    # Versioned filename embeds the timestamp + servers (PRD §11.5).
    assert result.manifest.database_filename == "2026-07-17T100000Z-en-cn.sqlite"
    # current.json is present and selects the immutable build.
    manifest_path = data_dir / CURRENT_MANIFEST_NAME
    assert manifest_path.is_file()
    on_disk = CurrentManifest.from_json(manifest_path.read_text(encoding="utf-8"))
    assert on_disk == result.manifest


def test_manifest_carries_prd_fields(tmp_path: Path) -> None:
    candidate = _make_candidate(tmp_path / "candidate.sqlite")
    result = promote_candidate(
        candidate, data_dir=tmp_path / "data", validation_passed=True, timestamp=_ts(10)
    )
    manifest = result.manifest
    assert manifest.schema_version == "0011_alias_locale"  # latest migration
    assert manifest.analyzer_version == ANALYZER_VERSION
    assert manifest.database_hash and len(manifest.database_hash) == 64
    assert manifest.snapshots[0]["source_id"] == "arknights_assets_gamedata"
    assert manifest.snapshots[0]["manifest_hash"] == "manifest-hash-A"
    assert manifest.created_at == "2026-07-17T10:00:00+00:00"


def test_resolve_active_database_points_at_promoted_build(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    candidate = _make_candidate(tmp_path / "candidate.sqlite")
    result = promote_candidate(
        candidate, data_dir=data_dir, validation_passed=True, timestamp=_ts(10)
    )
    active = resolve_active_database(data_dir)
    assert active == result.database_path
    assert resolve_active_database(tmp_path / "empty") is None


# --- unchanged -> no-op ----------------------------------------------------


def test_identical_content_is_noop(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    first = _make_candidate(tmp_path / "first.sqlite")
    r1 = promote_candidate(first, data_dir=data_dir, validation_passed=True, timestamp=_ts(10))

    manifest_before = (data_dir / CURRENT_MANIFEST_NAME).read_text(encoding="utf-8")
    # A second candidate: byte-different (fresh build, different applied_at) but
    # logically identical snapshots.
    second = _make_candidate(tmp_path / "second.sqlite")
    r2 = promote_candidate(second, data_dir=data_dir, validation_passed=True, timestamp=_ts(11))

    assert r2.status == "noop"
    assert r2.manifest == r1.manifest
    # No new build file, manifest untouched (§V4: no needless churn).
    builds = list((data_dir / BUILDS_DIRNAME).glob("*.sqlite"))
    assert len(builds) == 1
    assert (data_dir / CURRENT_MANIFEST_NAME).read_text(encoding="utf-8") == manifest_before


def test_changed_content_promotes_new_build(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    first = _make_candidate(tmp_path / "first.sqlite")
    promote_candidate(first, data_dir=data_dir, validation_passed=True, timestamp=_ts(10))

    changed = _make_candidate(
        tmp_path / "changed.sqlite",
        snapshots=[("arknights_assets_gamedata", "en", "manifest-hash-B")],
    )
    r2 = promote_candidate(changed, data_dir=data_dir, validation_passed=True, timestamp=_ts(11))

    assert r2.status == "promoted"
    assert r2.manifest.database_filename == "2026-07-17T110000Z-en-cn.sqlite"
    active = resolve_active_database(data_dir)
    assert active == r2.database_path


# --- retain N previous versions -------------------------------------------


def test_retention_prunes_oldest_beyond_n(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    results = []
    for hour in range(10, 15):  # 5 distinct promotions
        cand = _make_candidate(
            tmp_path / f"c{hour}.sqlite",
            snapshots=[("arknights_assets_gamedata", "en", f"hash-{hour}")],
        )
        results.append(
            promote_candidate(
                cand,
                data_dir=data_dir,
                validation_passed=True,
                retain_versions=3,
                timestamp=_ts(hour),
            )
        )

    builds = sorted(p.name for p in (data_dir / BUILDS_DIRNAME).glob("*.sqlite"))
    # Only the newest 3 survive.
    assert builds == [
        "2026-07-17T120000Z-en-cn.sqlite",
        "2026-07-17T130000Z-en-cn.sqlite",
        "2026-07-17T140000Z-en-cn.sqlite",
    ]
    # The last promotion reports the two files it pruned.
    assert set(results[-1].pruned) == {"2026-07-17T110000Z-en-cn.sqlite"}
    # The current build is always retained.
    assert resolve_active_database(data_dir) == results[-1].database_path


def test_retain_one_keeps_only_current(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    for hour in (10, 11):
        cand = _make_candidate(
            tmp_path / f"c{hour}.sqlite",
            snapshots=[("arknights_assets_gamedata", "en", f"hash-{hour}")],
        )
        last = promote_candidate(
            cand, data_dir=data_dir, validation_passed=True, retain_versions=1, timestamp=_ts(hour)
        )
    builds = list((data_dir / BUILDS_DIRNAME).glob("*.sqlite"))
    assert len(builds) == 1
    assert builds[0].name == last.manifest.database_filename


# --- §V3: fail closed, never replace the current DB -----------------------


def test_unvalidated_candidate_refused(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    candidate = _make_candidate(tmp_path / "candidate.sqlite")
    with pytest.raises(PromotionError, match="validation"):
        promote_candidate(candidate, data_dir=data_dir, validation_passed=False)
    # current.json must not be created (current DB, if any, stays active).
    assert not (data_dir / CURRENT_MANIFEST_NAME).exists()


def test_failed_candidate_leaves_current_db_active(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    good = _make_candidate(tmp_path / "good.sqlite")
    promoted = promote_candidate(good, data_dir=data_dir, validation_passed=True, timestamp=_ts(10))
    manifest_before = (data_dir / CURRENT_MANIFEST_NAME).read_text(encoding="utf-8")

    # A later sync fails validation -> must not touch current.json (§V3).
    later = _make_candidate(
        tmp_path / "later.sqlite",
        snapshots=[("arknights_assets_gamedata", "en", "hash-new")],
    )
    with pytest.raises(PromotionError):
        promote_candidate(later, data_dir=data_dir, validation_passed=False, timestamp=_ts(11))

    assert (data_dir / CURRENT_MANIFEST_NAME).read_text(encoding="utf-8") == manifest_before
    assert resolve_active_database(data_dir) == promoted.database_path


def test_candidate_without_schema_refused(tmp_path: Path) -> None:
    # An empty SQLite file (no migrations applied) is schema-incompatible (§V3).
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(str(empty)).close()
    with pytest.raises(PromotionError, match="schema"):
        promote_candidate(empty, data_dir=tmp_path / "data", validation_passed=True)
    assert not (tmp_path / "data" / CURRENT_MANIFEST_NAME).exists()


def test_missing_candidate_refused(tmp_path: Path) -> None:
    with pytest.raises(PromotionError, match="not found"):
        promote_candidate(
            tmp_path / "nope.sqlite", data_dir=tmp_path / "data", validation_passed=True
        )


# --- manifest round-trip + immutability -----------------------------------


def test_read_current_manifest_round_trip(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    candidate = _make_candidate(tmp_path / "candidate.sqlite")
    result = promote_candidate(
        candidate, data_dir=data_dir, validation_passed=True, timestamp=_ts(10)
    )
    loaded = read_current_manifest(data_dir / CURRENT_MANIFEST_NAME)
    assert loaded == result.manifest
    # Corrupt manifest -> treated as unpromoted, not an exception.
    (data_dir / CURRENT_MANIFEST_NAME).write_text("{ not json", encoding="utf-8")
    assert read_current_manifest(data_dir / CURRENT_MANIFEST_NAME) is None


def test_active_build_bytes_not_mutated_by_noop(tmp_path: Path) -> None:
    # §V4: the active DB is never mutated in place, including across a no-op.
    data_dir = tmp_path / "data"
    first = _make_candidate(tmp_path / "first.sqlite")
    r1 = promote_candidate(first, data_dir=data_dir, validation_passed=True, timestamp=_ts(10))
    build_bytes = r1.database_path.read_bytes()

    second = _make_candidate(tmp_path / "second.sqlite")
    promote_candidate(second, data_dir=data_dir, validation_passed=True, timestamp=_ts(11))

    assert r1.database_path.read_bytes() == build_bytes


def test_no_temp_residue_left_behind(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    candidate = _make_candidate(tmp_path / "candidate.sqlite")
    promote_candidate(candidate, data_dir=data_dir, validation_passed=True, timestamp=_ts(10))
    # Atomic writers must leave no ``.tmp`` residue in data/ or data/builds/.
    assert not list(data_dir.glob("*.tmp"))
    assert not list((data_dir / BUILDS_DIRNAME).glob("*.tmp"))


def test_current_json_is_valid_indented_json(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    candidate = _make_candidate(tmp_path / "candidate.sqlite")
    promote_candidate(candidate, data_dir=data_dir, validation_passed=True, timestamp=_ts(10))
    text = (data_dir / CURRENT_MANIFEST_NAME).read_text(encoding="utf-8")
    parsed = json.loads(text)
    assert parsed["database_filename"] == "2026-07-17T100000Z-en-cn.sqlite"
