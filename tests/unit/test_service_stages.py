"""T17: the M0 internal ``analyze_stage`` service (§V6, §V14).

Drives the pinned 4-4 fixture through a candidate DB and calls the service the
way both transports will (§V14 -- one shared domain function). Verifies:

* the result carries region + provenance on the facts (§V5) and the analyzer's
  evidence-backed observations with every §V6 field intact;
* the service is a pure, read-only function of ``(DB, input)`` -- two identical
  calls compare equal and the connection records no writes (§V2, §V14).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.analyzers.rules.aerial import RULE_ID
from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.enemies import import_enemies
from arknights_mcp.importers.stages import import_stages
from arknights_mcp.services.stages import analyze_stage
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
    connection = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(connection)
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, server="en")
    import_enemies(connection, adapter, SNAPSHOT_ID)
    import_stages(connection, adapter, SNAPSHOT_ID)
    connection.commit()
    return connection


def test_returns_stage_facts_with_region_and_provenance(conn: sqlite3.Connection) -> None:
    result = analyze_stage(conn, server="en", stage_code="4-4")
    assert result.status == "ok"
    assert result.server == "en"  # region (§V5)
    assert result.stage is not None
    assert result.stage.server == "en"
    assert result.stage.game_id == "main_04-04"
    assert result.stage.stage_code == "4-4"
    assert result.stage.sanity_cost == 18
    assert result.stage.zone_game_id == "main_4"
    # §V5: provenance (snapshot_id, imported_at) present on the factual response.
    assert result.stage.provenance.snapshot_id == SNAPSHOT_ID
    assert result.stage.provenance.imported_at == IMPORTED_AT


def test_occurrences_from_stage_enemies(conn: sqlite3.Connection) -> None:
    result = analyze_stage(conn, server="en", stage_code="4-4")
    by_id = {occ.game_id: occ for occ in result.occurrences}
    assert set(by_id) == {"enemy_1007_slime", "enemy_1105_drone"}

    drone = by_id["enemy_1105_drone"]
    assert drone.total_count == 2
    assert drone.motion_type == "FLY"
    assert drone.is_elite is True
    assert drone.first_spawn_time == 8.0
    # §V47: the occurrence carries the per-enemy stat block (level 0 fixture values).
    assert drone.hp == 900
    assert drone.atk == 260
    assert drone.def_ == 0
    assert drone.res == 10
    assert drone.attack_interval == 1.6
    assert drone.move_speed == 1.0
    assert drone.weight == 1

    slug = by_id["enemy_1007_slime"]
    assert slug.total_count == 3
    assert slug.motion_type == "WALK"
    assert slug.hp == 1650
    assert slug.def_ == 100


def test_aerial_observation_carries_v6_fields(conn: sqlite3.Connection) -> None:
    result = analyze_stage(conn, server="en", stage_code="4-4")
    assert len(result.observations) == 1
    obs = result.observations[0]
    # §V6: every mandated field present + well-formed, propagated by the service.
    assert obs.rule_id == RULE_ID
    assert obs.analyzer_version == result.analyzer_version
    assert result.analyzer_version is not None
    assert 0.0 <= obs.confidence <= 1.0
    assert obs.confidence >= 0.9  # authoritative motion_type=FLY
    assert obs.evidence  # non-empty typed evidence
    assert isinstance(obs.limitations, tuple)
    # Evidence traces to the flying drone only, from the typed motion field.
    assert {e.ref for e in obs.evidence} == {"enemy_1105_drone"}
    assert obs.evidence[0].field == "motion_type"
    assert obs.evidence[0].value == "FLY"
    assert result.warnings == ()


def test_lookup_by_game_id_matches_stage_code(conn: sqlite3.Connection) -> None:
    by_code = analyze_stage(conn, server="en", stage_code="4-4")
    by_id = analyze_stage(conn, server="en", game_id="main_04-04")
    assert by_id == by_code


def test_not_found_returns_typed_status(conn: sqlite3.Connection) -> None:
    result = analyze_stage(conn, server="en", stage_code="9-9")
    assert result.status == "not_found"
    assert result.stage is None
    assert result.occurrences == ()
    assert result.observations == ()
    assert result.analyzer_version is None


def test_wrong_region_is_not_found(conn: sqlite3.Connection) -> None:
    # §V5: en data is not surfaced under a cn query.
    result = analyze_stage(conn, server="cn", stage_code="4-4")
    assert result.status == "not_found"


def test_requires_a_selector(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="stage_code or game_id"):
        analyze_stage(conn, server="en")


def test_shared_core_is_deterministic_and_read_only(conn: sqlite3.Connection) -> None:
    # §V14: same DB + same input -> identical domain result.
    changes_before = conn.total_changes
    first = analyze_stage(conn, server="en", stage_code="4-4")
    second = analyze_stage(conn, server="en", stage_code="4-4")
    assert first == second
    # §V2: the service only reads -- no writes recorded on the connection.
    assert conn.total_changes == changes_before
