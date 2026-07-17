"""T11: snapshot manifest/checksum + provenance construction (§V17).

Every imported record must carry snapshot_id + source_path/key +
transform_version + record_hash; the manifest hash must be deterministic and
change when content changes (enables no-op detection and fail-closed rebuilds).
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers import enemies as enemies_mod
from arknights_mcp.importers import manifest as manifest_mod
from arknights_mcp.importers import stages as stages_mod
from arknights_mcp.importers.field_policy import FIELD_POLICY_VERSION
from arknights_mcp.importers.manifest import (
    TRANSFORM_VERSION,
    build_manifest,
    insert_record_provenance,
    make_record_provenance,
    make_snapshot_id,
    make_snapshot_record,
)
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.util.hashing import record_hash


def _adapter(tmp_path: Path, payload: dict[str, str]) -> LocalSnapshotAdapter:
    root = tmp_path / "en"
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in payload.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return LocalSnapshotAdapter(root, server="en")


def test_manifest_is_deterministic(tmp_path: Path) -> None:
    a1 = _adapter(tmp_path / "s1", {"gamedata/excel/stage_table.json": '{"a":1}'})
    a2 = _adapter(tmp_path / "s2", {"gamedata/excel/stage_table.json": '{"a":1}'})
    m1 = build_manifest(a1)
    m2 = build_manifest(a2)
    assert m1.manifest_hash == m2.manifest_hash
    assert set(m1.files) == {"gamedata/excel/stage_table.json"}


def test_manifest_changes_with_content(tmp_path: Path) -> None:
    a1 = _adapter(tmp_path / "s1", {"f.json": '{"a":1}'})
    a2 = _adapter(tmp_path / "s2", {"f.json": '{"a":2}'})
    assert build_manifest(a1).manifest_hash != build_manifest(a2).manifest_hash


def test_snapshot_id_format() -> None:
    assert make_snapshot_id("en", "abcdef0123456789").startswith("en:")
    assert make_snapshot_id("en", "abcdef0123456789") == "en:abcdef012345"


def test_snapshot_record_fields(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, {"f.json": "{}"})
    manifest = build_manifest(adapter)
    rec = make_snapshot_record(
        source_id="local_snapshot",
        server="en",
        manifest=manifest,
        license_status_at_import="no_explicit_dataset_license_assumed",
        imported_at="2026-07-17T00:00:00+00:00",
    )
    assert rec.snapshot_id.startswith("en:")
    assert rec.manifest_hash == manifest.manifest_hash
    assert rec.field_policy_version == FIELD_POLICY_VERSION
    assert rec.imported_at == "2026-07-17T00:00:00+00:00"
    assert rec.status == "imported"


def test_record_provenance_carries_all_v17_fields() -> None:
    record = {"enemyId": "enemy_1007_slime", "name": "Originium Slug"}
    prov = make_record_provenance(
        snapshot_id="en:abc123",
        source_path="gamedata/excel/enemy_handbook_table.json",
        source_record_key="enemy_1007_slime",
        record=record,
    )
    assert prov.snapshot_id == "en:abc123"
    assert prov.source_path.endswith("enemy_handbook_table.json")
    assert prov.source_record_key == "enemy_1007_slime"
    assert prov.record_hash == record_hash(record)
    assert prov.transform_version == TRANSFORM_VERSION
    assert prov.field_policy_version == FIELD_POLICY_VERSION


def test_record_hash_is_stable_and_distinct() -> None:
    assert record_hash({"a": 1, "b": 2}) == record_hash({"b": 2, "a": 1})
    assert record_hash({"a": 1}) != record_hash({"a": 2})


def _seed_snapshot(conn: sqlite3.Connection, snapshot_id: str = "en:test000000") -> str:
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


def test_insert_record_provenance_writes_v17_row(tmp_path: Path) -> None:
    # V17/V37: the single shared insert helper writes every provenance column and
    # returns the DB-assigned provenance_id both importers link their rows to.
    conn = build_database(tmp_path / "cand.sqlite")
    snapshot_id = _seed_snapshot(conn)
    record = {"enemyId": "enemy_1007_slime", "name": "Originium Slug"}
    provenance_id = insert_record_provenance(
        conn,
        snapshot_id=snapshot_id,
        source_path="gamedata/excel/enemy_handbook_table.json",
        source_record_key="enemy_1007_slime",
        record=record,
    )
    conn.commit()
    assert isinstance(provenance_id, int)
    assert provenance_id > 0
    row = conn.execute(
        "SELECT snapshot_id, source_path, source_record_key, record_hash, "
        "transform_version, field_policy_version FROM record_provenance "
        "WHERE provenance_id = ?",
        (provenance_id,),
    ).fetchone()
    assert row == (
        snapshot_id,
        "gamedata/excel/enemy_handbook_table.json",
        "enemy_1007_slime",
        record_hash(record),
        TRANSFORM_VERSION,
        FIELD_POLICY_VERSION,
    )


def test_provenance_insert_has_single_home() -> None:
    # V37: the record_provenance INSERT lives in exactly one module (manifest);
    # both importers route through the shared helper, with no divergent copies.
    manifest_src = inspect.getsource(manifest_mod)
    enemies_src = inspect.getsource(enemies_mod)
    stages_src = inspect.getsource(stages_mod)

    assert manifest_src.count("INSERT INTO record_provenance") == 1
    assert "INSERT INTO record_provenance" not in enemies_src
    assert "INSERT INTO record_provenance" not in stages_src
    # The old per-module helper must be gone, not merely unused.
    assert "_insert_provenance" not in stages_src
    assert "insert_record_provenance" in enemies_src
    assert "insert_record_provenance" in stages_src
