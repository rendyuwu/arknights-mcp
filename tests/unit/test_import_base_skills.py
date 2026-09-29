"""Base (RIIC) skill importer (building_data.json -> base_skills + links; ADR 0021).

The fixture ``building_data.json`` is a verbatim upstream EN extract (Amiya + Bellone and
their five buffs, every upstream key kept), so the allowlist drop is proven on the real
shape. Bellone has no operator row in the fixture, which exercises the skipped-char path.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.db.purge import _purge_source_rows
from arknights_mcp.importers.base_skills import import_base_skills, parse_base_skills
from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "operator" / "en"
BUILDING = FIXTURE_ROOT / "gamedata" / "excel" / "building_data.json"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"


def _build(tmp_path: Path, root: Path = FIXTURE_ROOT) -> Path:
    path = tmp_path / "cand.sqlite"
    build_candidate(
        path,
        [ServerImport("en", LocalSnapshotAdapter(root, "en", "local_snapshot"), "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    return path


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608


def test_parse_keeps_name_room_and_clean_text_and_drops_display_metadata() -> None:
    raw = json.loads(BUILDING.read_text(encoding="utf-8"))
    # The fixture keeps every upstream key, so the drop below cannot pass vacuously.
    assert {"skillIcon", "buffColor", "targets"} <= set(raw["buffs"]["trade_ord_spd_ext[020]"])
    skills, _ = parse_base_skills(raw)
    alpha = next(s for s in skills if s.buff_id == "trade_ord_spd_ext[020]")
    assert alpha.display_name == "Famiglia Business α"
    assert alpha.room_type == "TRADING"
    assert alpha.description is not None
    assert "order acquisition efficiency +25%" in alpha.description
    assert "<" not in alpha.description
    for dropped in ("skillIcon", "buffColor", "targets", "buffIcon", "sortId"):
        assert dropped not in alpha.provenance_record


def test_parse_reads_slots_and_stages_in_order_with_unlock_gates() -> None:
    _, operators = parse_base_skills(json.loads(BUILDING.read_text(encoding="utf-8")))
    bellone = next(o for o in operators if o.char_id == "char_4037_demetr")
    assert [
        (s.slot_index, s.stage_index, s.buff_id, s.unlock_phase, s.unlock_level)
        for s in bellone.stages
    ] == [
        (1, 1, "trade_ord_spd_ext[020]", 0, 1),
        (1, 2, "trade_ord_spd_ext[021]", 2, 1),
        (2, 1, "trade_ord_limit&cost_P[020]", 0, 1),
    ]


def test_import_links_present_operators_and_skips_absent_ones(tmp_path: Path) -> None:
    conn = open_read_only(_build(tmp_path))
    try:
        assert _count(conn, "base_skills") == 5
        links = conn.execute(
            "SELECT o.game_id, l.slot_index, b.buff_id FROM operator_base_skills l "
            "JOIN operators o ON o.operator_pk = l.operator_pk "
            "JOIN base_skills b ON b.base_skill_pk = l.base_skill_pk ORDER BY l.slot_index"
        ).fetchall()
        # Bellone has no operator row in the fixture: skipped, never a dangling link.
        assert links == [
            ("char_002_amiya", 1, "control_tra_spd[000]"),
            ("char_002_amiya", 2, "dorm_rec_all[010]"),
        ]
    finally:
        conn.close()


def test_absent_building_data_imports_nothing(tmp_path: Path) -> None:
    root = tmp_path / "snap"
    (root / "gamedata" / "excel").mkdir(parents=True)
    candidate = sqlite3.connect(":memory:")
    result = import_base_skills(candidate, LocalSnapshotAdapter(root, "en"), "en:test")
    assert result.base_skills_inserted == 0
    assert result.operator_links_inserted == 0


def test_buffs_that_all_fail_to_parse_refuse_a_silent_empty_build(tmp_path: Path) -> None:
    root = tmp_path / "snap"
    excel = root / "gamedata" / "excel"
    excel.mkdir(parents=True)
    (excel / "building_data.json").write_text(
        json.dumps({"buffs": {"x": {"buffName": "n"}}, "chars": {}}), encoding="utf-8"
    )
    candidate = sqlite3.connect(":memory:")
    with pytest.raises(ImporterError, match="silent empty base-skill build"):
        import_base_skills(candidate, LocalSnapshotAdapter(root, "en"), "en:test")


def test_purge_cascades_the_base_skill_and_faction_rows(tmp_path: Path) -> None:
    conn = sqlite3.connect(_build(tmp_path))
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        assert _count(conn, "operator_factions") == 2
        _purge_source_rows(conn, "local_snapshot")
        conn.commit()
        for table in ("base_skills", "operator_base_skills", "operator_factions"):
            assert _count(conn, table) == 0, table
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_purge_of_a_build_predating_0021_completes(tmp_path: Path) -> None:
    conn = sqlite3.connect(_build(tmp_path))
    try:
        for table in ("operator_base_skills", "base_skills", "operator_factions"):
            conn.execute(f"DROP TABLE {table}")  # noqa: S608
        _purge_source_rows(conn, "local_snapshot")
        conn.commit()
        assert _count(conn, "operators") == 0
    finally:
        conn.close()
