"""§T35 ``get_enemy`` tool tests (§V5/§V23; §I.tool).

The tool is the model -> service -> envelope bridge for a single enemy lookup;
these drive it end to end against the same production read-only path (§V2) using
the pinned 4-4 fixture (which imports two enemies: a ground slug and an aerial
drone). They assert:

* the §V5 region + provenance ride every ``ok`` result, and an ``en`` enemy is
  never surfaced under a ``cn`` query (en/cn never mixed);
* the typed §V23 envelope shape, including fail-closed ``not_found`` /
  ``database_unavailable`` / ``internal_error`` with no path/trace leak;
* the §I.tool wire contract: a read-only spec with a bounded input schema.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from arknights_mcp.db.connection import DatabaseUnavailable, open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.envelopes import SCHEMA_VERSION
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools.enemy import build_get_enemy_spec
from arknights_mcp.models.common import MAX_ID_LEN
from arknights_mcp.services.enemies import get_enemy
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Build the 4-4 fixture candidate read-only (imports the two enemies)."""
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    return open_read_only(path)


def _handler(conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    return build_get_enemy_spec(lambda: conn).handler


# --- facts + level stat block -------------------------------------------------


def test_default_returns_enemy_facts_and_levels(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", game_id="enemy_1007_slime")
    assert env.status == "ok"
    assert env.schema_version == SCHEMA_VERSION
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert set(data) == {"enemy"}
    enemy = data["enemy"]
    assert enemy["game_id"] == "enemy_1007_slime"  # type: ignore[index]
    assert enemy["display_name"] == "Originium Slug"  # type: ignore[index]
    assert enemy["enemy_class"] == "NORMAL"  # type: ignore[index]
    assert enemy["is_boss"] is False  # type: ignore[index]
    assert enemy["motion_type"] == "WALK"  # type: ignore[index]
    levels = enemy["levels"]  # type: ignore[index]
    assert len(levels) == 1
    lvl = levels[0]
    assert lvl["level_variant"] == 0
    assert lvl["hp"] == 1650
    assert lvl["def"] == 100
    assert lvl["res"] == 0
    assert lvl["attack_interval"] == 2.0
    assert lvl["block_behavior"] == "blockable"
    # Structural JSON is decoded back to a Python object (§V18 vetted at import).
    assert lvl["abilities"] == []


def test_aerial_enemy_abilities_decoded(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", game_id="enemy_1105_drone")
    enemy = env.to_dict()["data"]["enemy"]  # type: ignore[index]
    assert enemy["is_elite"] is True
    assert enemy["motion_type"] == "FLY"
    lvl = enemy["levels"][0]  # type: ignore[index]
    assert lvl["res"] == 10
    assert lvl["abilities"] == ["aerial"]


# --- §V5 region + provenance --------------------------------------------------


def test_ok_carries_region_and_provenance(conn: sqlite3.Connection) -> None:
    # §V5: every factual response carries region + provenance.
    env = _handler(conn)(server="en", game_id="enemy_1007_slime")
    prov = env.to_dict()["provenance"]
    assert isinstance(prov, list) and len(prov) == 1
    assert prov[0]["server"] == "en"
    assert prov[0]["snapshot_id"]
    assert prov[0]["imported_at"]


def test_wrong_region_is_not_found(conn: sqlite3.Connection) -> None:
    # §V5: en data is not surfaced under a cn query -- en/cn never mixed.
    assert _handler(conn)(server="cn", game_id="enemy_1007_slime").status == "not_found"


# --- §V23 typed failures ------------------------------------------------------


def test_not_found_envelope(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", game_id="enemy_9999_ghost")
    assert env.status == "not_found"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert data["message"] == "no enemy matched the given region and game_id"
    # §V24: a not_found never suggests a query-time download/scrape.
    assert "download" not in data["suggested_action"].lower()  # type: ignore[union-attr]
    assert "scrape" not in data["suggested_action"].lower()  # type: ignore[union-attr]


def test_database_unavailable_envelope() -> None:
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: cand.sqlite")

    env = build_get_enemy_spec(boom).handler(server="en", game_id="enemy_1007_slime")
    assert env.status == "database_unavailable"
    data = env.to_dict()["data"]
    # §V23: no local path / file name leaks into the client-facing message.
    assert data["message"] == "the active database is unavailable"  # type: ignore[index]
    assert "cand.sqlite" not in str(data)


def test_unexpected_error_fails_closed_to_internal_error() -> None:
    def boom() -> sqlite3.Connection:
        raise RuntimeError("secret path /home/ubuntu/db.sqlite blew up")

    env = build_get_enemy_spec(boom).handler(server="en", game_id="enemy_1007_slime")
    assert env.status == "internal_error"
    # §V23: the fixed message carries no exception text / stack trace / local path.
    assert str(env.to_dict()["data"]).find("/home/ubuntu") == -1
    assert "blew up" not in str(env.to_dict()["data"])


# --- §V18 input gate ----------------------------------------------------------


def test_unknown_parameter_rejected(conn: sqlite3.Connection) -> None:
    # §V18: extra="forbid" -> a crafted request cannot smuggle an unknown field.
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", game_id="enemy_1007_slime", include_levels=False)


def test_missing_game_id_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(conn)(server="en")


def test_bad_region_rejected(conn: sqlite3.Connection) -> None:
    # §V5: server is constrained to en|cn.
    with pytest.raises(ValidationError):
        _handler(conn)(server="jp", game_id="enemy_1007_slime")


def test_over_length_game_id_rejected(conn: sqlite3.Connection) -> None:
    # §V18: an over-length id cannot carry an oversized blob.
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", game_id="x" * (MAX_ID_LEN + 1))


# --- §V2 read-only / §I.tool wire contract ------------------------------------


def test_service_is_read_only(conn: sqlite3.Connection) -> None:
    # §V2: the service only reads -- no writes recorded on the connection.
    before = conn.total_changes
    get_enemy(conn, server="en", game_id="enemy_1007_slime")
    assert conn.total_changes == before


def test_spec_registers_read_only_with_bounded_schema(conn: sqlite3.Connection) -> None:
    reg = ToolRegistry()
    spec = reg.register(build_get_enemy_spec(lambda: conn))
    assert reg.names() == ("get_enemy",)
    assert spec.read_only is True
    tool = spec.to_mcp_tool()
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True
    # §V18: unknown params forbidden + the game_id length cap rides the wire (§V5
    # requires server).
    assert tool.inputSchema["additionalProperties"] is False
    assert set(tool.inputSchema["required"]) == {"server", "game_id"}
    assert tool.inputSchema["properties"]["game_id"]["maxLength"] == MAX_ID_LEN
