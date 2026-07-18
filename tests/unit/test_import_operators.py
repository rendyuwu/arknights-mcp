"""T42: operator + skill + talent importer (character_table, skill_table, aliases).

Parses the real id-keyed ``character_table`` / ``skill_table`` shapes into the
operator domain with the field allowlist + sanitization (§V18/§V31), per-record
provenance (§V17), and fail-closed constraint handling (§V33). Prose fields
(``description``) are excluded and ``gameplay_description`` stays ``NULL`` (§V16).
"""

from __future__ import annotations

import json as _json
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.operators import (
    ParsedAlias,
    ParsedOperator,
    import_operators,
    insert_operators,
    parse_operators,
    parse_skills,
)
from arknights_mcp.importers.search_index import build_search_index
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

DESCRIPTION_PROSE = "A long lore blurb that must never be imported into the database."

CHARACTER = {
    "char_002_amiya": {
        "name": "Amiya",
        "appellation": "Amiya",
        "description": DESCRIPTION_PROSE,
        "rarity": "TIER_5",
        "profession": "CASTER",
        "subProfessionId": "corecaster",
        "position": "RANGED",
        "tagList": ["Arts", "Nuker\x00\t"],  # control chars must be sanitized
        "isNotObtainable": False,
        "phases": [
            {
                "rangeId": "1-1",
                "maxLevel": 50,
                "attributesKeyFrames": [
                    {"level": 1, "data": {"maxHp": 649, "atk": 259}},
                    {
                        "level": 50,
                        "data": {
                            "maxHp": 1043,
                            "atk": 481,
                            "def": 88,
                            "magicResistance": 10,
                            "cost": 15,
                            "blockCnt": 1,
                            "baseAttackTime": 1.6,
                            "respawnTime": 70,
                        },
                    },
                ],
            },
            {
                "rangeId": "1-2",
                "maxLevel": 80,
                "attributesKeyFrames": [
                    {"level": 80, "data": {"maxHp": 1400, "atk": 700, "cost": 17}},
                ],
            },
        ],
        "skills": [
            {"skillId": "skchr_amiya_1", "unlockCond": {"phase": "PHASE_0", "level": 1}},
            {"skillId": "skchr_amiya_2", "unlockCond": {"phase": "PHASE_1", "level": 1}},
        ],
        "talents": [
            {
                "candidates": [
                    {
                        "unlockCondition": {"phase": "PHASE_0", "level": 1},
                        "requiredPotentialRank": 0,
                        "name": "Nervous Impulse",
                        "description": DESCRIPTION_PROSE,
                        "blackboard": [{"key": "atk_scale", "value": 1.1, "valueStr": None}],
                    },
                    {
                        "unlockCondition": {"phase": "PHASE_2", "level": 1},
                        "requiredPotentialRank": 3,
                        "name": "Nervous Impulse",
                        "description": DESCRIPTION_PROSE,
                        "blackboard": [{"key": "atk_scale", "value": 1.2, "valueStr": None}],
                    },
                ]
            }
        ],
    },
    "token_10012_shield": {  # a summon token: must be skipped (profession TOKEN)
        "name": "Guardian",
        "rarity": "TIER_1",
        "profession": "TOKEN",
        "position": "MELEE",
        "phases": [],
    },
    "trap_001_crate": {  # a map trap: must be skipped (profession TRAP)
        "name": "Crate",
        "profession": "TRAP",
        "phases": [],
    },
}

SKILLS = {
    "skchr_amiya_1": {
        "skillId": "skchr_amiya_1",
        "levels": [
            {
                "name": "Arts Charge",
                "skillType": "PASSIVE",
                "durationType": "NONE",
                "description": DESCRIPTION_PROSE,
                "duration": 0.0,
                "rangeId": None,
                "spData": {"spType": "INCREASE_WITH_TIME", "spCost": 0, "initSp": 0},
                "blackboard": [{"key": "charge", "value": 1.0, "valueStr": None}],
            },
            {
                "name": "Arts Charge",
                "skillType": "PASSIVE",
                "durationType": "NONE",
                "description": DESCRIPTION_PROSE,
                "duration": 0.0,
                "rangeId": None,
                "spData": {"spType": "INCREASE_WITH_TIME", "spCost": 0, "initSp": 0},
                "blackboard": [],
            },
        ],
    },
    "skchr_amiya_2": {
        "skillId": "skchr_amiya_2",
        "levels": [
            {
                "name": "Chain Cast",
                "skillType": "MANUAL",
                "durationType": "NONE",
                "description": DESCRIPTION_PROSE,
                "duration": 20.0,
                "rangeId": "x-1",
                "spData": {"spType": "INCREASE_WITH_TIME", "spCost": 30, "initSp": 0},
                "blackboard": [{"key": "atk", "value": 1.5, "valueStr": None}],
            }
        ],
    },
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


def _adapter(tmp_path: Path, *, with_skills: bool = True, character: dict | None = None) -> Path:
    root = tmp_path / "en"
    (root / "gamedata" / "excel").mkdir(parents=True)
    (root / "gamedata" / "excel" / "character_table.json").write_text(
        _json.dumps(CHARACTER if character is None else character), encoding="utf-8"
    )
    if with_skills:
        (root / "gamedata" / "excel" / "skill_table.json").write_text(
            _json.dumps(SKILLS), encoding="utf-8"
        )
    return root


# --- parsing -----------------------------------------------------------------


def test_parse_reads_typed_fields_and_skips_tokens_and_traps() -> None:
    parsed = {o.game_id: o for o in parse_operators(CHARACTER)}
    assert set(parsed) == {"char_002_amiya"}  # token + trap skipped
    amiya = parsed["char_002_amiya"]
    assert amiya.rarity == 5  # TIER_5 -> 5
    assert amiya.profession == "CASTER"
    assert amiya.subclass_id == "corecaster"
    assert amiya.position == "RANGED"
    assert amiya.obtainable is True
    assert "Arts" in amiya.tags
    # phase stats come from the max-level keyframe.
    assert amiya.phases[0].max_hp == 1043
    assert amiya.phases[0].block_count == 1
    assert amiya.phases[1].max_level == 80
    # skill links: slot is 1-based, unlockCond.phase "PHASE_1" -> 1.
    assert amiya.skill_links[0].skill_game_id == "skchr_amiya_1"
    assert amiya.skill_links[0].slot_index == 1
    assert amiya.skill_links[1].unlock_phase == 1
    # talent: name kept (short label), two variants, potential rank read.
    assert amiya.talents[0].display_name == "Nervous Impulse"
    assert amiya.talents[0].variants[1].unlock_phase == 2
    assert amiya.talents[0].variants[1].potential_rank == 3


def test_parse_excludes_prose() -> None:
    amiya = parse_operators(CHARACTER)[0]
    assert DESCRIPTION_PROSE not in str(amiya.provenance_record)


def test_parse_sanitizes_nested_tag_string() -> None:
    amiya = parse_operators(CHARACTER)[0]
    assert "Nuker" in amiya.tags
    assert "\x00" not in "".join(amiya.tags)  # §V31 nested string leaf sanitized


def test_parse_skills_reads_typed_fields() -> None:
    skills = {s.game_id: s for s in parse_skills(SKILLS)}
    s1 = skills["skchr_amiya_1"]
    assert s1.display_name == "Arts Charge"
    assert s1.skill_type == "PASSIVE"
    assert s1.sp_type == "INCREASE_WITH_TIME"
    assert s1.duration_type == "NONE"
    assert [lv.level for lv in s1.levels] == [1, 2]
    assert skills["skchr_amiya_2"].levels[0].sp_cost == 30


# --- insertion ---------------------------------------------------------------


def test_import_via_adapter_populates_all_tables(tmp_path: Path) -> None:
    root = _adapter(tmp_path)
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    result = import_operators(conn, LocalSnapshotAdapter(root, server="en"), "en:test000000")
    conn.commit()
    assert result.operators_inserted == 1
    assert result.skills_inserted == 2
    assert result.phases_inserted == 2
    assert result.talents_inserted == 1
    assert result.skill_links_inserted == 2
    assert result.aliases_inserted == 1  # name == appellation -> single alias
    assert conn.execute("SELECT COUNT(*) FROM operator_phases").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM operator_skills").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM skill_levels").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM talent_levels").fetchone()[0] == 2
    rarity = conn.execute(
        "SELECT rarity, profession FROM operators WHERE game_id='char_002_amiya'"
    ).fetchone()
    assert rarity == (5, "CASTER")


def test_provenance_attached_to_operators_and_skills(tmp_path: Path) -> None:
    root = _adapter(tmp_path)
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    import_operators(conn, LocalSnapshotAdapter(root, server="en"), "en:test000000")
    conn.commit()
    assert conn.execute("SELECT provenance_id FROM operators").fetchone()[0] is not None
    assert conn.execute("SELECT provenance_id FROM skills").fetchone()[0] is not None


def test_no_prose_and_gameplay_description_null(tmp_path: Path) -> None:
    root = _adapter(tmp_path)
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    import_operators(conn, LocalSnapshotAdapter(root, server="en"), "en:test000000")
    conn.commit()
    dump = "\n".join(
        str(row)
        for table in (
            "operators",
            "operator_phases",
            "skills",
            "skill_levels",
            "talents",
            "talent_levels",
            "record_provenance",
        )
        for row in conn.execute(f"SELECT * FROM {table}")
    )
    assert DESCRIPTION_PROSE not in dump  # §V16/§V18 prose excluded
    # gameplay_description columns default to NULL (§V16).
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM skill_levels WHERE gameplay_description IS NOT NULL"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM talent_levels WHERE gameplay_description IS NOT NULL"
        ).fetchone()[0]
        == 0
    )


def test_tag_json_is_sanitized(tmp_path: Path) -> None:
    root = _adapter(tmp_path)
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    import_operators(conn, LocalSnapshotAdapter(root, server="en"), "en:test000000")
    conn.commit()
    tag_json = conn.execute(
        "SELECT tag_json FROM operators WHERE game_id='char_002_amiya'"
    ).fetchone()[0]
    assert "\x00" not in tag_json
    assert "Nuker" in tag_json


def test_missing_character_table_is_graceful(tmp_path: Path) -> None:
    # A combat-only snapshot without character_table imports zero operators rather
    # than failing (operator domain is optional per snapshot).
    root = tmp_path / "en"
    (root / "gamedata" / "excel").mkdir(parents=True)
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    result = import_operators(conn, LocalSnapshotAdapter(root, server="en"), "en:test000000")
    assert result.operators_inserted == 0
    assert result.skills_inserted == 0


def test_link_to_absent_skill_is_skipped(tmp_path: Path) -> None:
    # An operator naming a skill absent from skill_table skips the link instead of
    # violating the operator_skills.skill_pk FK.
    character = {
        "char_x": {
            "name": "X",
            "profession": "CASTER",
            "phases": [],
            "skills": [{"skillId": "skchr_absent", "unlockCond": {"phase": "PHASE_0", "level": 1}}],
            "talents": [],
        }
    }
    root = _adapter(tmp_path, character=character)
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    result = import_operators(conn, LocalSnapshotAdapter(root, server="en"), "en:test000000")
    conn.commit()
    assert result.operators_inserted == 1
    assert result.skill_links_inserted == 0
    assert conn.execute("SELECT COUNT(*) FROM operator_skills").fetchone()[0] == 0


def test_duplicate_operator_fails_gracefully(tmp_path: Path) -> None:
    # A repeated (server, game_id) collides on UNIQUE and must raise a typed
    # ImporterError, not an uncaught sqlite3.IntegrityError (§V33).
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    dup = ParsedOperator(
        game_id="char_dup",
        display_name="Dup",
        rarity=6,
        profession="GUARD",
        subclass_id=None,
        position="MELEE",
        tags=[],
        obtainable=True,
        aliases=[ParsedAlias("Dup", "name", "dup")],
        phases=[],
        skill_links=[],
        talents=[],
        provenance_record={"character": {"name": "Dup"}},
    )
    with pytest.raises(ImporterError, match="collides"):
        insert_operators(
            conn,
            [dup, dup],
            skill_pk_by_game_id={},
            server="en",
            snapshot_id="en:test000000",
            character_source_path="gamedata/excel/character_table.json",
        )


def test_operator_and_aliases_feed_search_index(tmp_path: Path) -> None:
    # T42 aliases scope: operators + aliases populate the unified FTS index.
    root = _adapter(tmp_path)
    conn = build_database(tmp_path / "cand.sqlite")
    _seed_snapshot(conn)
    import_operators(conn, LocalSnapshotAdapter(root, server="en"), "en:test000000")
    build_search_index(conn)
    conn.commit()
    hit = conn.execute(
        "SELECT entity_type, name FROM entity_fts WHERE entity_type='operator'"
    ).fetchone()
    assert hit == ("operator", "Amiya")
    matched = conn.execute(
        "SELECT COUNT(*) FROM entity_fts WHERE entity_fts MATCH 'Amiya'"
    ).fetchone()[0]
    assert matched == 1
