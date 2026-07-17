"""T27: the ``get_data_status`` + ``get_data_sources`` services (§V27; §V2/§V14).

Both are shared domain entry points that read the active database read-only and
return serializable, public-safe results. ``get_data_sources`` must never leak
secrets, local paths, OAuth config, or policy notes (§V27), while still carrying
the PRD §13.10 fields (private-hosting + redistribution posture) and the active
snapshot commit/version.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from arknights_mcp.db.connection import read_only_connection
from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.pipeline import ServerImport, build_candidate, seed_data_sources
from arknights_mcp.services.source_status import get_data_sources
from arknights_mcp.services.status import get_data_status
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"
IMPORTED_AT = "2026-07-10T00:00:00+00:00"
NOW = datetime(2026, 7, 17, tzinfo=UTC)


def _active_db(tmp_path: Path) -> Path:
    path = tmp_path / "active.sqlite"
    build_candidate(
        path,
        [
            ServerImport(
                "en", LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot"), "local_snapshot"
            )
        ],
        registry=load_source_registry(REGISTRY),
        imported_at=IMPORTED_AT,
    )
    return path


# --- get_data_status ----------------------------------------------------------


def test_data_status_accepts_naive_now(tmp_path: Path) -> None:
    # L11: an injected timezone-naive ``now`` is normalized to UTC instead of
    # raising TypeError in the age subtraction.
    naive = datetime(2026, 7, 17)  # noqa: DTZ001 - intentionally naive for the test
    with read_only_connection(_active_db(tmp_path)) as conn:
        status = get_data_status(conn, mode="local", now=naive)
    assert status.snapshots
    assert all(s.age_days is not None and s.age_days >= 0 for s in status.snapshots)


def test_data_status_reports_active_snapshot(tmp_path: Path) -> None:
    with read_only_connection(_active_db(tmp_path)) as conn:
        status = get_data_status(conn, mode="local", now=NOW)

    assert status.status == "ok"
    assert status.schema_version is not None
    assert status.analyzer_version
    assert status.mode == "local"
    assert {"enemies", "stages"} <= set(status.supported_domains)
    assert len(status.snapshots) == 1
    snap = status.snapshots[0]
    assert snap.server == "en"
    assert snap.source_id == "local_snapshot"
    assert snap.age_days == 7  # 2026-07-17 - 2026-07-10
    # serializable for the tool envelope / CLI --json
    json.dumps(status.to_dict())


def test_data_status_empty_db_is_data_stale(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite"
    conn = build_database(path)
    seed_data_sources(conn, load_source_registry(REGISTRY))
    conn.commit()
    conn.close()

    with read_only_connection(path) as conn2:
        status = get_data_status(conn2, now=NOW)

    assert status.status == "data_stale"
    assert status.snapshots == ()
    assert status.warnings
    assert status.suggested_action is not None
    assert "sync" in status.suggested_action or "import" in status.suggested_action


# --- get_data_sources ---------------------------------------------------------


def test_data_sources_public_view_with_active_snapshot(tmp_path: Path) -> None:
    registry = load_source_registry(REGISTRY)
    with read_only_connection(_active_db(tmp_path)) as conn:
        result = get_data_sources(registry, conn)

    by_id = {s.source_id: s for s in result.sources}
    assert by_id["arknights_assets_gamedata"].enabled is True
    assert by_id["penguin_statistics"].enabled is False

    local = by_id["local_snapshot"]
    assert len(local.active_snapshots) == 1
    assert local.active_snapshots[0].server == "en"
    assert local.active_snapshots[0].snapshot_id


def test_data_sources_is_public_safe(tmp_path: Path) -> None:
    """§V27: no secrets, no local filesystem paths, no policy notes -- but the
    PRD §13.10 private-hosting/redistribution posture is present."""
    registry = load_source_registry(REGISTRY)
    with read_only_connection(_active_db(tmp_path)) as conn:
        dumped = json.dumps(get_data_sources(registry, conn).to_dict())

    assert "policy_notes" not in dumped
    # No local filesystem path from the machine registry / staging leaks out.
    assert str(REPO_ROOT) not in dumped
    assert str(tmp_path) not in dumped
    # PRD §13.10 posture fields are present.
    assert "private_hosting_status" in dumped
    assert "redistribution_status" in dumped


def test_data_sources_without_conn_has_no_snapshots() -> None:
    registry = load_source_registry(REGISTRY)
    result = get_data_sources(registry)
    assert result.sources
    assert all(s.active_snapshots == () for s in result.sources)
