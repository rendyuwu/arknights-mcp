"""``find_operators`` (ADR 0021) over the operator fixture build.

The EN fixture carries Amiya's base skills (CONTROL slot 1, DORMITORY slot 2), her
factions (rhodes main, rim secondary), and a collab flag of false. The CN fixture carries
none of the three files, so CN answers are the empty build arm.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.support.account import account_fixture_store

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.envelopes import ResponseEnvelope
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.mcp.tools._shared import FACTION_NOT_FOUND_MESSAGE
from arknights_mcp.mcp.tools.find_operators import build_find_operators_spec
from arknights_mcp.mcp.tools.operator import build_get_operator_spec
from arknights_mcp.services.base_skills import (
    BASE_SKILL_DOMAIN_MISSING_LIMITATION,
    BASE_SKILL_SLOT_NOTE,
    COLLAB_LIMITATION,
    FIND_EMPTY_LIMITATION,
    find_operators,
)
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "operator"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

_AGREEMENT = (
    "When this Operator is assigned to the Control Center, all Trading Posts' order "
    "efficiency +7% (only the most effective one will take effect when assigned Operators "
    "have the same skill effect)"
)


def _build(tmp_path: Path, en_root: Path = FIXTURES / "en") -> Path:
    path = tmp_path / "cand.sqlite"
    roots = {"en": en_root, "cn": FIXTURES / "cn"}
    build_candidate(
        path,
        [
            ServerImport(s, LocalSnapshotAdapter(root, s, "local_snapshot"), "local_snapshot")
            for s, root in roots.items()
        ],
        registry=load_source_registry(REGISTRY),
    )
    return path


def _with_handbook(tmp_path: Path, handbook: dict[str, object]) -> sqlite3.Connection:
    """The fixture build with Amiya's ``handbook_info_table`` entry replaced."""
    root = tmp_path / "en"
    shutil.copytree(FIXTURES / "en", root)
    (root / "gamedata" / "excel" / "handbook_info_table.json").write_text(
        json.dumps({"handbookDict": handbook, "npcDict": {}}), encoding="utf-8"
    )
    return open_read_only(_build(tmp_path, root))


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return open_read_only(_build(tmp_path))


def _call(conn: sqlite3.Connection, **params: object) -> ResponseEnvelope:
    return build_find_operators_spec(lambda: conn).handler(**params)


def test_room_type_lists_the_operators_with_that_facilitys_skills(conn: sqlite3.Connection) -> None:
    env = _call(conn, server="en", room_type="CONTROL")
    assert env.status == "ok"
    assert env.data["room_type"] == "CONTROL"
    (amiya,) = env.data["operators"]
    assert amiya["game_id"] == "char_002_amiya"
    assert amiya["base_skills"] == [
        {
            "slot": 1,
            "display_name": "Agreement",
            "description": _AGREEMENT,
            "unlock_elite": 0,
            "unlock_level": 1,
        }
    ]
    assert BASE_SKILL_SLOT_NOTE in env.limitations


def test_a_facility_nobody_serves_is_an_ok_empty_page(conn: sqlite3.Connection) -> None:
    # A current build that matches nothing never tells the client to ask for a sync.
    env = _call(conn, server="en", room_type="TRADING")
    assert env.status == "ok"
    assert env.data["operators"] == []
    assert FIND_EMPTY_LIMITATION in env.limitations
    assert BASE_SKILL_DOMAIN_MISSING_LIMITATION not in env.limitations


@pytest.mark.parametrize("faction", ["Rim Billiton", "RIM", "rim"])
def test_faction_matches_a_secondary_faction_by_id_or_name(
    conn: sqlite3.Connection, faction: str
) -> None:
    env = _call(conn, server="en", faction=faction)
    assert env.status == "ok"
    assert env.data["faction_id"] == "rim"
    (amiya,) = env.data["operators"]
    assert amiya["factions"] == [
        {"faction_id": "rhodes", "display_name": "Rhodes Island", "main": True},
        {"faction_id": "rim", "display_name": "Rim Billiton"},
    ]
    assert "base_skills" not in amiya


def test_an_unknown_faction_is_not_found(conn: sqlite3.Connection) -> None:
    env = _call(conn, server="en", faction="nowhere")
    assert env.status == "not_found"
    assert env.data["message"] == FACTION_NOT_FOUND_MESSAGE


def test_collab_false_keeps_a_flagged_non_collab_operator(conn: sqlite3.Connection) -> None:
    only = _call(conn, server="en", collab=True)
    assert only.status == "ok"
    assert only.data["operators"] == []
    assert COLLAB_LIMITATION in only.limitations
    dropped = _call(conn, server="en", faction="rhodes", collab=False)
    (amiya,) = dropped.data["operators"]
    assert amiya["collab"] is False


def test_collab_true_keeps_a_collab_operator_and_false_drops_it(tmp_path: Path) -> None:
    conn = _with_handbook(tmp_path, {"char_002_amiya": {"isLimited": True}})
    (amiya,) = _call(conn, server="en", collab=True).data["operators"]
    assert (amiya["game_id"], amiya["collab"]) == ("char_002_amiya", True)
    assert _call(conn, server="en", faction="rhodes", collab=False).data["operators"] == []
    op = build_get_operator_spec(lambda: conn).handler(server="en", game_id="char_002_amiya")
    assert op.data["operator"]["summary"]["collab"] is True
    assert COLLAB_LIMITATION in op.limitations


def test_an_unknown_collab_flag_matches_neither_value(tmp_path: Path) -> None:
    conn = _with_handbook(tmp_path, {})
    (amiya,) = _call(conn, server="en", faction="rhodes").data["operators"]
    assert "collab" not in amiya
    for collab in (True, False):
        env = _call(conn, server="en", faction="rhodes", collab=collab)
        assert env.data["operators"] == [], collab


@pytest.mark.parametrize("params", [{}, {"collab": False}])
def test_a_request_without_a_narrowing_filter_is_rejected(
    conn: sqlite3.Connection, params: dict[str, object]
) -> None:
    with pytest.raises(ValidationError, match="set room_type or faction, or collab to true"):
        _call(conn, server="en", **params)
    with pytest.raises(ValueError, match="set room_type or faction, or collab to true"):
        find_operators(conn, server="en", **params)  # type: ignore[arg-type]


def test_regions_never_mix(conn: sqlite3.Connection) -> None:
    env = _call(conn, server="cn", room_type="CONTROL")
    assert env.status == "ok"
    assert env.data["operators"] == []


def test_a_build_predating_0021_degrades_to_empty_answers(tmp_path: Path) -> None:
    # An active build synced before migration 0021 has none of the new tables or the
    # collab column: every new read answers empty or not_found, never internal_error,
    # and an empty answer says the build needs a sync.
    path = _build(tmp_path)
    raw = sqlite3.connect(path)
    for table in ("operator_base_skills", "base_skills", "operator_factions"):
        raw.execute(f"DROP TABLE {table}")  # noqa: S608
    raw.execute("ALTER TABLE operators DROP COLUMN collab")
    raw.commit()
    raw.close()
    conn = open_read_only(path)
    tools = build_tool_registry(
        lambda: conn,
        registry=load_source_registry(REGISTRY),
        mode="local",
        account_store=account_fixture_store(),
    )

    op = tools.get("get_operator").handler(
        server="en", game_id="char_002_amiya", include_base_skills=True
    )
    assert op.status == "ok"
    assert "base_skills" not in op.data["operator"]
    assert "factions" not in op.data["operator"]["summary"]
    assert BASE_SKILL_DOMAIN_MISSING_LIMITATION in op.limitations

    found = tools.get("find_operators").handler(server="en", room_type="CONTROL")
    assert found.status == "ok"
    assert found.data["operators"] == []
    assert found.limitations.count(BASE_SKILL_DOMAIN_MISSING_LIMITATION) == 1
    assert FIND_EMPTY_LIMITATION not in found.limitations

    roster = tools.get("get_my_roster").handler(server="en", room_type="CONTROL")
    assert roster.status == "ok"
    assert BASE_SKILL_DOMAIN_MISSING_LIMITATION in roster.limitations

    by_faction = tools.get("get_my_roster").handler(server="en", faction="rim")
    assert by_faction.status == "not_found"
    assert by_faction.data["message"] == FACTION_NOT_FOUND_MESSAGE
