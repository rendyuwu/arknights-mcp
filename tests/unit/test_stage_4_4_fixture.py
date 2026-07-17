"""T15: the minimal on-disk 4-4 fixture under ``tests/fixtures/stage_4_4``.

The fixture is a *hand-authored, minimal* snapshot tree — only the fields the M0
importer + analyzer tests require, never a full game-data dump (§V16). Unlike
T14 (which drives the parsers with inline dicts), this fixture is the canonical,
reusable snapshot that downstream M0 tasks (T16 threat rule, T17 service, T18
accept test) build a candidate DB from.

These tests keep the fixture honest:

* **minimal (§V16, §V18)** — each tabular record carries only allowlisted,
  test-required fields; no prose/story keys anywhere; record counts stay tiny;
  the whole tree is orders of magnitude smaller than a real dump.
* **valid & reusable (§V17)** — importing the fixture yields the 4-4 stage, its
  two enemy occurrences, and per-record provenance.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.enemies import import_enemies
from arknights_mcp.importers.field_policy import (
    ENEMY_HANDBOOK_ALLOWLIST,
    ENEMY_LEVEL_ALLOWLIST,
    STAGE_ALLOWLIST,
    ZONE_ALLOWLIST,
)
from arknights_mcp.importers.stages import import_stages
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"

#: Every file that makes up the minimal snapshot tree, relative to FIXTURE_ROOT.
FIXTURE_FILES = (
    "gamedata/excel/enemy_handbook_table.json",
    "gamedata/excel/zone_table.json",
    "gamedata/excel/stage_table.json",
    "gamedata/levels/enemydata/enemy_database.json",
    "gamedata/levels/main/level_main_04-04.json",
)

#: Free-text / prose keys that must never appear in a minimal fixture (§V16).
FORBIDDEN_PROSE_KEYS = frozenset(
    {
        "description",
        "storyText",
        "loreText",
        "unlockText",
        "flavorText",
        "briefing",
        "endText",
        "startText",
    }
)

#: A real gamedata dump is megabytes; the whole minimal tree must stay tiny.
MAX_FIXTURE_BYTES = 64 * 1024


def _load(relative_path: str) -> Any:
    return json.loads((FIXTURE_ROOT / relative_path).read_text(encoding="utf-8"))


def _iter_keys(node: Any) -> list[str]:
    """Recursively collect every mapping key in a JSON document."""
    keys: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            keys.append(key)
            keys.extend(_iter_keys(value))
    elif isinstance(node, list):
        for item in node:
            keys.extend(_iter_keys(item))
    return keys


def _seed_snapshot(conn: sqlite3.Connection, snapshot_id: str = "en:fixture0000") -> str:
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
        (snapshot_id, "local_snapshot", "en", "2026-07-17T00:00:00+00:00", "mh", "imported", "1"),
    )
    conn.commit()
    return snapshot_id


def _import_fixture(tmp_path: Path) -> sqlite3.Connection:
    conn = build_database(tmp_path / "cand.sqlite")
    snapshot_id = _seed_snapshot(conn)
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, server="en")
    import_enemies(conn, adapter, snapshot_id)
    import_stages(conn, adapter, snapshot_id)
    conn.commit()
    return conn


# --- minimality: only test-required fields, no full dump (§V16, §V18) ---------


def test_fixture_tree_present() -> None:
    assert FIXTURE_ROOT.is_dir()
    for rel in FIXTURE_FILES:
        assert (FIXTURE_ROOT / rel).is_file(), f"missing fixture file: {rel}"


def test_no_prose_keys_anywhere() -> None:
    for rel in FIXTURE_FILES:
        keys = set(_iter_keys(_load(rel)))
        leaked = keys & FORBIDDEN_PROSE_KEYS
        assert not leaked, f"prose key(s) {sorted(leaked)} in {rel} (§V16)"


def test_records_only_carry_allowlisted_fields() -> None:
    """Tabular records hold only fields the importer keeps (§V18): the fixture is
    the allowlist, not a dump of every source field."""
    for entry in _load("gamedata/excel/enemy_handbook_table.json")["enemyData"].values():
        assert set(entry) <= ENEMY_HANDBOOK_ALLOWLIST
    for enemy in _load("gamedata/levels/enemydata/enemy_database.json")["enemies"].values():
        for level in enemy["levels"]:
            assert set(level) <= ENEMY_LEVEL_ALLOWLIST
    for zone in _load("gamedata/excel/zone_table.json")["zones"].values():
        assert set(zone) <= ZONE_ALLOWLIST
    for stage in _load("gamedata/excel/stage_table.json")["stages"].values():
        assert set(stage) <= STAGE_ALLOWLIST


def test_record_counts_stay_tiny() -> None:
    assert len(_load("gamedata/excel/enemy_handbook_table.json")["enemyData"]) <= 4
    assert len(_load("gamedata/levels/enemydata/enemy_database.json")["enemies"]) <= 4
    assert len(_load("gamedata/excel/zone_table.json")["zones"]) == 1
    assert len(_load("gamedata/excel/stage_table.json")["stages"]) == 1


def test_fixture_is_not_a_dump() -> None:
    total = sum((FIXTURE_ROOT / rel).stat().st_size for rel in FIXTURE_FILES)
    assert total < MAX_FIXTURE_BYTES, f"fixture too large ({total} bytes); is it a dump? (§V16)"


# --- valid & reusable: imports into stage + occurrences + provenance (§V17) ---


def test_fixture_imports_stage_enemies_and_provenance(tmp_path: Path) -> None:
    conn = _import_fixture(tmp_path)

    stage = conn.execute(
        "SELECT stage_code, sanity_cost, provenance_id FROM stages "
        "WHERE server='en' AND game_id='main_04-04'"
    ).fetchone()
    assert stage is not None
    assert stage[0] == "4-4"
    assert stage[1] == 18
    assert stage[2] is not None  # §V17 provenance attached

    occ = conn.execute(
        "SELECT e.game_id, se.total_count FROM stage_enemies se "
        "JOIN enemies e ON e.enemy_pk = se.enemy_pk "
        "JOIN stages s ON s.stage_pk = se.stage_pk "
        "WHERE s.stage_code='4-4' ORDER BY e.game_id"
    ).fetchall()
    assert {row[0]: row[1] for row in occ} == {
        "enemy_1007_slime": 3,
        "enemy_1105_drone": 2,
    }

    # The aerial drone is preserved so the M0 threat rule (T16) has evidence.
    drone = conn.execute(
        "SELECT motion_type, is_elite FROM enemies WHERE game_id='enemy_1105_drone'"
    ).fetchone()
    assert drone == ("FLY", 1)

    assert conn.execute("SELECT COUNT(*) FROM record_provenance").fetchone()[0] > 0
