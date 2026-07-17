"""T11: snapshot manifest/checksum + provenance construction (§V17).

Every imported record must carry snapshot_id + source_path/key +
transform_version + record_hash; the manifest hash must be deterministic and
change when content changes (enables no-op detection and fail-closed rebuilds).
"""

from __future__ import annotations

from pathlib import Path

from arknights_mcp.importers.field_policy import FIELD_POLICY_VERSION
from arknights_mcp.importers.manifest import (
    TRANSFORM_VERSION,
    build_manifest,
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
