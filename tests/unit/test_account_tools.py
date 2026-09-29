"""Account roster tools (ADR 0020): ``get_my_roster`` / ``get_my_operator`` /
``get_my_inventory`` over the operator fixture build + the synthetic account store."""

from __future__ import annotations

import shutil
import socket
import sqlite3
from pathlib import Path

import pytest
from tests.support.account import account_fixture_path, account_fixture_store

from arknights_mcp.db.account import AccountStore
from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.envelopes import ResponseEnvelope
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.mcp.tools._shared import FACTION_NOT_FOUND_MESSAGE
from arknights_mcp.mcp.tools.account import ACCOUNT_UNAVAILABLE_MESSAGE
from arknights_mcp.services.account import (
    ACCOUNT_EMPTY_ROSTER_LIMITATION,
    ACCOUNT_UNKNOWN_OPERATOR_LIMITATION,
    ACCOUNT_UNNAMED_PART_LIMITATION,
    ROSTER_BASE_SKILL_NOTE,
)
from arknights_mcp.services.base_skills import BASE_SKILL_DOMAIN_MISSING_LIMITATION
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "operator" / "en"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "cand.sqlite"
    build_candidate(
        path,
        [
            ServerImport(
                "en", LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot"), "local_snapshot"
            )
        ],
        registry=load_source_registry(REGISTRY),
    )
    return open_read_only(path)


def _registry(conn: sqlite3.Connection, store: AccountStore | None) -> ToolRegistry:
    return build_tool_registry(
        lambda: conn, registry=load_source_registry(REGISTRY), mode="local", account_store=store
    )


def _call(
    conn: sqlite3.Connection, name: str, store: AccountStore | None = None, **params: object
) -> ResponseEnvelope:
    return (
        _registry(conn, store if store is not None else account_fixture_store())
        .get(name)
        .handler(**params)
    )


def _module_type(conn: sqlite3.Connection, module_id: str) -> str:
    row = conn.execute("select module_type from modules where game_id = ?", (module_id,)).fetchone()
    return str(row[0])


def test_roster_enriches_owned_operators_from_the_build(conn: sqlite3.Connection) -> None:
    env = _call(conn, "get_my_roster", server="en")
    assert env.status == "ok"
    amiya, synthetic = env.data["operators"]
    assert amiya["game_id"] == "char_002_amiya"
    assert amiya["rarity"] == 5
    assert amiya["masteries"] == [3, 2]
    assert amiya["modules"] == [
        {
            "module_id": "uniequip_002_amiya",
            "module_type": _module_type(conn, "uniequip_002_amiya"),
            "level": 3,
        }
    ]
    assert amiya["equipped_module_id"] == "uniequip_002_amiya"
    assert synthetic["game_id"] == "char_9999_synthetic"
    assert "display_name" not in synthetic
    assert ACCOUNT_UNKNOWN_OPERATOR_LIMITATION in env.limitations
    assert ACCOUNT_UNNAMED_PART_LIMITATION not in env.limitations
    assert env.provenance[0].snapshot_id.startswith("yostar-en-")


def test_a_filter_that_matches_nothing_is_an_ok_empty_list(conn: sqlite3.Connection) -> None:
    env = _call(conn, "get_my_roster", server="en", min_rarity=6)
    assert env.status == "ok"
    assert env.data["operators"] == []
    assert ACCOUNT_EMPTY_ROSTER_LIMITATION in env.limitations


def test_room_type_keeps_owned_operators_with_that_facility_and_marks_the_stage_in_effect(
    conn: sqlite3.Connection,
) -> None:
    env = _call(conn, "get_my_roster", server="en", room_type="DORMITORY")
    assert env.status == "ok"
    assert env.data["room_type"] == "DORMITORY"
    (amiya,) = env.data["operators"]
    assert (amiya["game_id"], amiya["elite"], amiya["level"]) == ("char_002_amiya", 2, 80)
    assert [(s["slot"], s["display_name"], s["in_effect"]) for s in amiya["base_skills"]] == [
        (2, "Violin Solo", True)
    ]
    assert ROSTER_BASE_SKILL_NOTE in env.limitations


def test_faction_drops_operators_this_build_does_not_place_in_it(
    conn: sqlite3.Connection,
) -> None:
    env = _call(conn, "get_my_roster", server="en", faction="rim")
    assert env.status == "ok"
    assert [o["game_id"] for o in env.data["operators"]] == ["char_002_amiya"]
    assert "base_skills" not in env.data["operators"][0]


def test_an_unknown_roster_faction_is_not_found(conn: sqlite3.Connection) -> None:
    env = _call(conn, "get_my_roster", server="en", faction="nowhere")
    assert env.status == "not_found"
    assert env.data["message"] == FACTION_NOT_FOUND_MESSAGE


def test_a_facility_no_owned_operator_serves_is_a_plain_empty_list(
    conn: sqlite3.Connection,
) -> None:
    # A current build that matches nothing never tells the client to ask for a sync.
    env = _call(conn, "get_my_roster", server="en", room_type="TRADING")
    assert env.status == "ok"
    assert env.data["operators"] == []
    assert ACCOUNT_EMPTY_ROSTER_LIMITATION in env.limitations
    assert BASE_SKILL_DOMAIN_MISSING_LIMITATION not in env.limitations


def test_operator_detail_marks_alternate_forms_and_owned_skins(conn: sqlite3.Connection) -> None:
    env = _call(conn, "get_my_operator", server="en", game_id="char_002_amiya")
    assert env.status == "ok"
    operator = env.data["operator"]
    by_id = {s["skill_id"]: s for s in operator["skills"]}
    assert by_id["skchr_amiya2_1"]["form_id"] == "char_1001_amiya2"
    assert "form_id" not in by_id["skchr_amiya_1"]
    skins = {s["skin_id"]: s for s in operator["skins"]}
    assert set(skins) == {"char_002_amiya#1", "char_002_amiya@epoque#4", "char_1001_amiya2#2"}
    assert skins["char_1001_amiya2#2"]["alt_form"] is True
    assert "alt_form" not in skins["char_002_amiya#1"]
    assert [m["equipped"] for m in operator["modules"]] == [True]


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"server": "en", "game_id": "char_999_ghost"}, "not_found"),
        ({"server": "cn", "game_id": "char_002_amiya"}, "unsupported_server"),
    ],
)
def test_operator_lookup_failures_are_typed(
    conn: sqlite3.Connection, params: dict[str, object], status: str
) -> None:
    assert _call(conn, "get_my_operator", **params).status == status


@pytest.mark.parametrize("tool", ["get_my_roster", "get_my_inventory"])
def test_no_reachable_roster_is_database_unavailable(
    conn: sqlite3.Connection, tmp_path: Path, tool: str
) -> None:
    for store in (None, AccountStore(f"sqlite:///{tmp_path}/empty.sqlite")):
        env = _registry(conn, store).get(tool).handler(server="en")
        assert env.status == "database_unavailable"
        assert env.data["message"] == ACCOUNT_UNAVAILABLE_MESSAGE


def test_a_roster_from_another_schema_is_schema_incompatible(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    copy = tmp_path / "old.sqlite"
    shutil.copy(account_fixture_path(), copy)
    raw = sqlite3.connect(copy)
    raw.execute("update account_roster set schema_version = '0'")
    raw.commit()
    raw.close()
    env = _call(conn, "get_my_roster", AccountStore(f"sqlite:///{copy}"), server="en")
    assert env.status == "schema_incompatible"


def test_the_tools_never_touch_the_network(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = account_fixture_store()

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network")

    monkeypatch.setattr(socket, "socket", refuse)
    for name, params in (
        ("get_my_roster", {"server": "en"}),
        ("get_my_operator", {"server": "en", "game_id": "char_002_amiya"}),
        ("get_my_inventory", {"server": "en"}),
    ):
        assert _call(conn, name, store, **params).status == "ok"
