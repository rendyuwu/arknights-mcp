"""The stage read repository.

Drives :class:`StageRepository` against the pinned 4-4 fixture through a database
**opened read-only** (the production path). Verifies the repository:

* returns the stage keyed by ``stage_code`` and by ``game_id`` identically, with
  its joined region provenance;
* keeps ``en`` and ``cn`` separate -- a ``cn`` lookup of an ``en`` stage is
  ``None`` (never silently mixed);
* returns the stage's typed enemy occurrences from ``stage_enemies``;
* only reads -- the connection records no writes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.db.migrations import build_database
from arknights_mcp.db.repositories.stages import StageRepository
from arknights_mcp.importers.enemies import import_enemies
from arknights_mcp.importers.stages import import_stages
from arknights_mcp.services.stages import MAX_STAGE_CODE_MATCHES
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
SNAPSHOT_ID = "en:fixture0000"
IMPORTED_AT = "2026-07-17T00:00:00+00:00"


def _seed_snapshot(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "local_snapshot",
            "Local",
            "op",
            "local://x",
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
        (SNAPSHOT_ID, "local_snapshot", "en", IMPORTED_AT, "mh", "imported", "1"),
    )
    conn.commit()


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Build + import the 4-4 fixture, then reopen the file read-only."""
    db_path = tmp_path / "cand.sqlite"
    writer = build_database(db_path)
    _seed_snapshot(writer)
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, server="en")
    import_enemies(writer, adapter, SNAPSHOT_ID)
    import_stages(writer, adapter, SNAPSHOT_ID)
    writer.commit()
    writer.close()
    return open_read_only(db_path)


def test_stages_by_code_carries_provenance(conn: sqlite3.Connection) -> None:
    repo = StageRepository(conn)
    # A stage_code is not unique, so the read returns the whole matching
    # set (bounded) in pick order and the service discloses; the fixture holds one.
    (stage,) = repo.stages_by_code("en", "4-4", MAX_STAGE_CODE_MATCHES)
    assert stage.server == "en"
    assert stage.game_id == "main_04-04"
    assert stage.stage_code == "4-4"
    assert stage.sanity_cost == 18
    assert stage.zone_game_id == "main_4"
    # Region provenance joined onto the factual row.
    assert stage.snapshot_id == SNAPSHOT_ID
    assert stage.imported_at == IMPORTED_AT


def test_stage_by_game_id_matches_by_code(conn: sqlite3.Connection) -> None:
    repo = StageRepository(conn)
    assert [repo.stage_by_game_id("en", "main_04-04")] == repo.stages_by_code(
        "en", "4-4", MAX_STAGE_CODE_MATCHES
    )


def test_wrong_region_returns_none(conn: sqlite3.Connection) -> None:
    # A cn lookup does not surface en data.
    repo = StageRepository(conn)
    assert repo.stages_by_code("cn", "4-4", MAX_STAGE_CODE_MATCHES) == []
    assert repo.stage_by_game_id("cn", "main_04-04") is None


def test_absent_stage_returns_none(conn: sqlite3.Connection) -> None:
    repo = StageRepository(conn)
    assert repo.stages_by_code("en", "9-9", MAX_STAGE_CODE_MATCHES) == []


def test_stage_enemies_typed_occurrences(conn: sqlite3.Connection) -> None:
    repo = StageRepository(conn)
    (stage,) = repo.stages_by_code("en", "4-4", MAX_STAGE_CODE_MATCHES)
    by_id = {e.game_id: e for e in repo.stage_enemies(stage.stage_pk)}
    assert set(by_id) == {"enemy_1007_slime", "enemy_1105_drone"}

    drone = by_id["enemy_1105_drone"]
    assert drone.total_count == 2
    assert drone.motion_type == "FLY"
    assert drone.is_elite is True  # typed bool, not raw 0/1
    assert drone.first_spawn_time == 8.0

    slime = by_id["enemy_1007_slime"]
    assert slime.total_count == 3
    assert slime.is_boss is False


def test_repository_is_read_only(conn: sqlite3.Connection) -> None:
    # Repository queries never write.
    before = conn.total_changes
    repo = StageRepository(conn)
    (stage,) = repo.stages_by_code("en", "4-4", MAX_STAGE_CODE_MATCHES)
    repo.stage_enemies(stage.stage_pk)
    assert conn.total_changes == before
