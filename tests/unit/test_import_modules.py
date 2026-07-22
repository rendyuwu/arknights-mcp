"""T43: module importer (uniequip_table, uniequip_data, battle_equip_table).

Parses the real ``uniequip_table`` (``equipDict`` + ``charEquip``) and
``battle_equip_table`` shapes into the module domain with the field allowlist +
sanitization (§V18/§V31), per-record provenance (§V17), and fail-closed constraint
handling (§V33). The trait/talent-change effect-description TEMPLATE (mechanic text
referencing the change's blackboard keys) is imported into the change bundles (§V65
(a)/ADR 0010); module-level lore (``uniEquipDesc``) stays excluded and
``module_levels.gameplay_description`` stays ``NULL`` (§V16). The ``INITIAL`` default
slot is skipped; a module whose ``charId`` is absent from the roster is skipped.
"""

from __future__ import annotations

import json as _json
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.modules import (
    ParsedModule,
    ParsedModuleLevel,
    import_modules,
    insert_modules,
    parse_modules,
)
from arknights_mcp.importers.operators import import_operators
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

PROSE = "A module lore blurb that must never be imported into the database."
#: §T127/§V65/ADR 0010: module trait/talent-change effect TEMPLATES (mechanic text
#: referencing the change's blackboard keys) ARE imported into the change bundles;
#: the module-level lore ``uniEquipDesc`` above stays excluded (§V16 ceiling).
TRAIT_TEMPLATE = "Increases ATK to <@ba.vup>{atk_scale:0%}</> when attacking."
TALENT_TEMPLATE = "Adds a <@ba.vup>{prob:0%}</> chance to recover extra SP on attack."

CHARACTER = {
    "char_002_amiya": {
        "name": "Amiya",
        "appellation": "Amiya",
        "profession": "CASTER",
        "position": "RANGED",
        "phases": [],
        "skills": [],
        "talents": [],
    }
}

UNIEQUIP = {
    "equipDict": {
        "uniequip_001_amiya": {  # INITIAL default slot: must be skipped
            "uniEquipId": "uniequip_001_amiya",
            "uniEquipName": "Original",
            "uniEquipDesc": PROSE,
            "charId": "char_002_amiya",
            "type": "INITIAL",
            "typeName1": "ORIGINAL",
            "typeName2": None,
            "unlockEvolvePhase": "PHASE_0",
            "unlockLevel": 0,
            "itemCost": None,
        },
        "uniequip_002_amiya": {
            "uniEquipId": "uniequip_002_amiya",
            "uniEquipName": "Magician's Trick\x00\t",  # control chars must be sanitized
            "uniEquipDesc": PROSE,
            "charId": "char_002_amiya",
            "type": "ADVANCED",
            "typeName1": "CX",
            "typeName2": "1",
            "unlockEvolvePhase": "PHASE_2",
            "unlockLevel": 40,
            "itemCost": {
                "1": [{"id": "mat_1", "count": 8, "type": "MATERIAL", "sortId": 99}],
                "2": [{"id": "mat_2", "count": 4, "type": "MATERIAL"}],
                "3": [{"id": "mat_3", "count": 2, "type": "MATERIAL"}],
            },
        },
        "uniequip_003_orphan": {  # charId absent from operators: skipped at insert
            "uniEquipId": "uniequip_003_orphan",
            "uniEquipName": "Orphan",
            "charId": "char_absent",
            "type": "ADVANCED",
            "typeName1": "XX",
            "unlockEvolvePhase": "PHASE_1",
            "unlockLevel": 30,
        },
    },
    "charEquip": {"char_002_amiya": ["uniequip_001_amiya", "uniequip_002_amiya"]},
}

BATTLE = {
    "uniequip_002_amiya": {
        "phases": [
            {
                "equipLevel": 1,
                "attributeBlackboard": [{"key": "atk", "value": 34, "valueStr": None}],
                "parts": [
                    {
                        "target": "TRAIT",
                        "overrideTraitDataBundle": {
                            "candidates": [
                                {
                                    "additionalDescription": TRAIT_TEMPLATE,
                                    "overrideDescripton": "ATK becomes {atk_scale:0%}.",
                                    "unlockCondition": {"phase": "PHASE_2", "level": 1},
                                    "requiredPotentialRank": 0,
                                    "blackboard": [{"key": "atk_scale", "value": 1.1}],
                                }
                            ]
                        },
                        "addOrOverrideTalentDataBundle": {"candidates": None},
                    }
                ],
            },
            {
                "equipLevel": 2,
                "attributeBlackboard": [{"key": "atk", "value": 48}],
                "parts": [
                    {
                        "target": "TALENT",
                        "overrideTraitDataBundle": {"candidates": None},
                        "addOrOverrideTalentDataBundle": {
                            "candidates": [
                                {
                                    "upgradeDescription": TALENT_TEMPLATE,
                                    "description": "Recovers extra SP ({prob:0%} chance).",
                                    "name": "Nervous Impulse",
                                    "talentIndex": 0,
                                    "unlockCondition": {"phase": "PHASE_2", "level": 1},
                                    "requiredPotentialRank": 0,
                                    "blackboard": [{"key": "prob", "value": 0.3}],
                                }
                            ]
                        },
                    }
                ],
            },
            {
                "equipLevel": 3,
                "attributeBlackboard": [
                    {"key": "atk", "value": 66},
                    {"key": "max_hp", "value": 150},
                ],
                "parts": [],
            },
        ]
    }
}


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
            "d",
        ),
    )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES (?,?,?,?,?,?,?)",
        (snapshot_id, "local_snapshot", "en", "2026-07-17T00:00:00+00:00", "mh", "imported", "1"),
    )
    conn.commit()
    return snapshot_id


def _adapter(tmp_path: Path, *, with_battle: bool = True, with_uniequip: bool = True) -> Path:
    root = tmp_path / "en"
    (root / "gamedata" / "excel").mkdir(parents=True)
    (root / "gamedata" / "excel" / "character_table.json").write_text(
        _json.dumps(CHARACTER), encoding="utf-8"
    )
    if with_uniequip:
        (root / "gamedata" / "excel" / "uniequip_table.json").write_text(
            _json.dumps(UNIEQUIP), encoding="utf-8"
        )
    if with_battle:
        (root / "gamedata" / "excel" / "battle_equip_table.json").write_text(
            _json.dumps(BATTLE), encoding="utf-8"
        )
    return root


# --- parsing -----------------------------------------------------------------


def test_parse_reads_typed_fields_and_skips_initial() -> None:
    parsed = {m.game_id: m for m in parse_modules(UNIEQUIP, BATTLE)}
    # INITIAL default slot skipped; the two ADVANCED modules parse (operator
    # resolution is deferred to insertion).
    assert set(parsed) == {"uniequip_002_amiya", "uniequip_003_orphan"}
    mod = parsed["uniequip_002_amiya"]
    assert mod.operator_game_id == "char_002_amiya"
    assert mod.module_type == "CX-1"  # typeName1 + typeName2
    assert mod.unlock_phase == 2  # PHASE_2 -> 2
    assert mod.unlock_level == 40
    assert [lv.level for lv in mod.levels] == [1, 2, 3]
    # level 1: stat bonus + a trait change; level 2: a talent change.
    lv1, lv2, lv3 = mod.levels
    assert lv1.stat_bonus == [{"key": "atk", "value": 34, "valueStr": None}]
    assert (
        lv1.trait_changes is not None
        and lv1.trait_changes[0]["blackboard"][0]["key"] == "atk_scale"
    )
    assert lv1.talent_changes is None
    assert lv2.talent_changes is not None and lv2.talent_changes[0]["talentIndex"] == 0
    assert lv3.stat_bonus is not None and {b["key"] for b in lv3.stat_bonus} == {"atk", "max_hp"}


def test_parse_excludes_prose() -> None:
    mod = {m.game_id: m for m in parse_modules(UNIEQUIP, BATTLE)}["uniequip_002_amiya"]
    assert PROSE not in str(mod.provenance_record)  # §V16/§V18 prose excluded


def test_parse_sanitizes_nested_display_name() -> None:
    mod = {m.game_id: m for m in parse_modules(UNIEQUIP, BATTLE)}["uniequip_002_amiya"]
    assert mod.display_name is not None
    assert "Magician" in mod.display_name
    assert "\x00" not in mod.display_name  # §V31 nested string leaf sanitized


def test_parse_item_cost_is_allowlisted() -> None:
    mod = {m.game_id: m for m in parse_modules(UNIEQUIP, BATTLE)}["uniequip_002_amiya"]
    cost1 = mod.levels[0].cost
    assert cost1 == [{"id": "mat_1", "count": 8, "type": "MATERIAL"}]  # sortId dropped
    assert mod.levels[2].cost == [{"id": "mat_3", "count": 2, "type": "MATERIAL"}]


# --- insertion ---------------------------------------------------------------


def _import_all(root: Path, conn: sqlite3.Connection):
    import_operators(conn, LocalSnapshotAdapter(root, server="en"), "en:test000000")
    result = import_modules(conn, LocalSnapshotAdapter(root, server="en"), "en:test000000")
    conn.commit()
    return result


def test_import_via_adapter_populates_modules(tmp_path: Path) -> None:
    root = _adapter(tmp_path)
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    result = _import_all(root, conn)
    # Only uniequip_002 links (INITIAL skipped, orphan operator skipped).
    assert result.modules_inserted == 1
    assert result.module_levels_inserted == 3
    row = conn.execute(
        "SELECT m.module_type, m.unlock_phase, m.unlock_level, o.game_id "
        "FROM modules m JOIN operators o ON o.operator_pk = m.operator_pk "
        "WHERE m.game_id = 'uniequip_002_amiya'"
    ).fetchone()
    assert row == ("CX-1", 2, 40, "char_002_amiya")
    assert conn.execute("SELECT COUNT(*) FROM module_levels").fetchone()[0] == 3


def test_orphan_operator_module_skipped(tmp_path: Path) -> None:
    # uniequip_003_orphan names char_absent -> the operator_pk FK cannot resolve,
    # so the module is skipped rather than inserted with a dangling reference.
    root = _adapter(tmp_path)
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    _import_all(root, conn)
    ids = [r[0] for r in conn.execute("SELECT game_id FROM modules")]
    assert ids == ["uniequip_002_amiya"]


def test_provenance_attached_to_modules(tmp_path: Path) -> None:
    root = _adapter(tmp_path)
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    _import_all(root, conn)
    assert conn.execute("SELECT provenance_id FROM modules").fetchone()[0] is not None


def test_lore_excluded_template_imported_gameplay_description_null(tmp_path: Path) -> None:
    # §T127/§V65/§V16 (ADR 0010): the module-level lore `uniEquipDesc` stays excluded
    # and `module_levels.gameplay_description` stays NULL, but the trait/talent-change
    # effect TEMPLATE rides the trait_changes_json/talent_changes_json bundle alongside
    # its blackboard for grounding.
    root = _adapter(tmp_path)
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    _import_all(root, conn)
    dump = "\n".join(
        str(row)
        for table in ("modules", "module_levels", "record_provenance")
        for row in conn.execute(f"SELECT * FROM {table}")
    )
    assert PROSE not in dump  # §V16 module lore excluded
    # §V16: the module templates live per-candidate in the change bundles, not the
    # gameplay_description column, which stays NULL for modules.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM module_levels WHERE gameplay_description IS NOT NULL"
        ).fetchone()[0]
        == 0
    )
    # §V65 (a): the trait template lands in trait_changes_json (level 1), the talent
    # template in talent_changes_json (level 2).
    trait_json, talent_json = conn.execute(
        "SELECT "
        "(SELECT trait_changes_json FROM module_levels ml JOIN modules m "
        " ON m.module_pk = ml.module_pk WHERE ml.level = 1), "
        "(SELECT talent_changes_json FROM module_levels ml JOIN modules m "
        " ON m.module_pk = ml.module_pk WHERE ml.level = 2)"
    ).fetchone()
    assert _json.loads(trait_json)[0]["description"] == TRAIT_TEMPLATE
    assert _json.loads(talent_json)[0]["description"] == TALENT_TEMPLATE


def test_parse_carries_change_template_alongside_blackboard() -> None:
    # §T127/§V65 (a): the parsed trait/talent change keeps its effect TEMPLATE next to
    # the blackboard (unit-testable without a DB); `overrideDescripton` is the fallback,
    # so `additionalDescription`/`upgradeDescription` win when present.
    mod = {m.game_id: m for m in parse_modules(UNIEQUIP, BATTLE)}["uniequip_002_amiya"]
    trait = mod.levels[0].trait_changes
    assert trait is not None and trait[0]["description"] == TRAIT_TEMPLATE
    assert trait[0]["blackboard"][0]["key"] == "atk_scale"  # emitted together
    talent = mod.levels[1].talent_changes
    assert talent is not None and talent[0]["description"] == TALENT_TEMPLATE
    assert talent[0]["blackboard"][0]["key"] == "prob"


def test_stat_and_cost_stored_as_json(tmp_path: Path) -> None:
    root = _adapter(tmp_path)
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    _import_all(root, conn)
    stat, cost = conn.execute(
        "SELECT stat_bonus_json, cost_json FROM module_levels ml "
        "JOIN modules m ON m.module_pk = ml.module_pk "
        "WHERE m.game_id = 'uniequip_002_amiya' AND ml.level = 1"
    ).fetchone()
    assert _json.loads(stat) == [{"key": "atk", "value": 34, "valueStr": None}]
    assert _json.loads(cost) == [{"count": 8, "id": "mat_1", "type": "MATERIAL"}]


def test_missing_uniequip_table_is_graceful(tmp_path: Path) -> None:
    # A snapshot without uniequip_table imports zero modules rather than failing
    # (module domain is optional per snapshot).
    root = _adapter(tmp_path, with_uniequip=False, with_battle=False)
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    result = _import_all(root, conn)
    assert result.modules_inserted == 0


def test_module_without_battle_data_still_imports_cost_levels(tmp_path: Path) -> None:
    # A module present in uniequip_table but absent from battle_equip_table still
    # yields its itemCost levels (no stat/trait/talent, but the level rows exist).
    root = _adapter(tmp_path, with_battle=False)
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    result = _import_all(root, conn)
    assert result.modules_inserted == 1
    assert result.module_levels_inserted == 3  # from itemCost keys "1"/"2"/"3"
    stat = conn.execute(
        "SELECT stat_bonus_json FROM module_levels ml JOIN modules m "
        "ON m.module_pk = ml.module_pk WHERE ml.level = 1"
    ).fetchone()[0]
    assert stat is None


def test_duplicate_module_fails_gracefully(tmp_path: Path) -> None:
    # A repeated (server, game_id) collides on UNIQUE and must raise a typed
    # ImporterError, not an uncaught sqlite3.IntegrityError (§V33).
    root = _adapter(tmp_path)
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    import_operators(conn, LocalSnapshotAdapter(root, server="en"), "en:test000000")
    conn.commit()
    dup = ParsedModule(
        game_id="uniequip_dup",
        operator_game_id="char_002_amiya",
        module_type="CX-1",
        display_name="Dup",
        unlock_phase=2,
        unlock_level=40,
        levels=[
            ParsedModuleLevel(
                level=1, stat_bonus=None, trait_changes=None, talent_changes=None, cost=None
            )
        ],
        provenance_record={"uniequip": {"uniEquipId": "uniequip_dup"}},
    )
    with pytest.raises(ImporterError, match="collides"):
        insert_modules(
            conn,
            [dup, dup],
            server="en",
            snapshot_id="en:test000000",
            uniequip_source_path="gamedata/excel/uniequip_table.json",
        )
