"""§T44 ``get_operator`` tool tests (§V5/§V22/§V23; §I.tool).

The tool is the model -> service -> envelope bridge for a single operator lookup;
these drive it end to end against the same production read-only path (§V2) using
the operator fixture (Amiya: two phases, two skills, one talent, one module). They
assert:

* the §V5 region + provenance ride every ``ok`` result, and an ``en`` operator is
  never surfaced under a ``cn`` query (en/cn never mixed);
* the §V22 default response is compact facts + summary + provenance -- the heavy
  phases/skills/talents/modules sections are opt-in include flags;
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
from arknights_mcp.mcp.tools.operator import build_get_operator_spec
from arknights_mcp.models.common import MAX_ID_LEN
from arknights_mcp.services.operators import get_operator
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "operator" / "en"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

_AMIYA = "char_002_amiya"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Build the operator fixture candidate read-only (imports Amiya + her module)."""
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    return open_read_only(path)


def _handler(conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    return build_get_operator_spec(lambda: conn).handler


# --- §V22 default = compact facts + summary + provenance ----------------------


def test_default_returns_summary_not_heavy_sections(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", game_id=_AMIYA)
    assert env.status == "ok"
    assert env.schema_version == SCHEMA_VERSION
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    op = data["operator"]
    assert op["game_id"] == _AMIYA  # type: ignore[index]
    assert op["display_name"] == "Amiya"  # type: ignore[index]
    # §V22: heavy sections are opt-in -- absent by default.
    assert set(op) == {"server", "game_id", "display_name", "summary", "provenance"}  # type: ignore[arg-type]
    summary = op["summary"]  # type: ignore[index]
    assert summary["rarity"] == 5
    assert summary["profession"] == "CASTER"
    # The summary advertises how many heavy sections exist without pulling them.
    assert summary["phase_count"] == 2
    assert summary["skill_count"] == 2
    assert summary["talent_count"] == 1
    assert summary["module_count"] == 1


def test_include_flags_add_bounded_sections(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(
        server="en",
        game_id=_AMIYA,
        include_phases=True,
        include_skills=True,
        include_talents=True,
        include_modules=True,
    )
    op = env.to_dict()["data"]["operator"]  # type: ignore[index]
    assert [p["phase"] for p in op["phases"]] == [0, 1]
    assert {s["game_id"] for s in op["skills"]} == {"skchr_amiya_1", "skchr_amiya_2"}
    # Skill levels + their decoded blackboard ride along (structural JSON vetted §V18).
    lv = op["skills"][0]["levels"][0]
    assert lv["level"] == 1 and lv["blackboard"] == [
        {"key": "charge", "value": 1.0, "valueStr": None}
    ]
    assert op["talents"][0]["display_name"] == "Nervous Impulse"
    mod = op["modules"][0]
    assert mod["module_type"] == "CX-1"
    assert mod["levels"][0]["cost"] == [{"id": "mat_1", "count": 8, "type": "MATERIAL"}]


def test_summary_can_be_dropped(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", game_id=_AMIYA, include_summary=False)
    op = env.to_dict()["data"]["operator"]  # type: ignore[index]
    assert "summary" not in op


# --- §V5 region + provenance --------------------------------------------------


def test_ok_carries_region_and_provenance(conn: sqlite3.Connection) -> None:
    # §V5: every factual response carries region + provenance on the envelope.
    env = _handler(conn)(server="en", game_id=_AMIYA)
    prov = env.to_dict()["provenance"]
    assert isinstance(prov, list) and len(prov) == 1
    assert prov[0]["server"] == "en"
    assert prov[0]["snapshot_id"].startswith("en:")
    assert prov[0]["imported_at"]


def test_include_provenance_only_toggles_the_in_data_echo(conn: sqlite3.Connection) -> None:
    # §V5 is unconditional: the envelope keeps its provenance even when the in-data
    # echo is turned off; the flag only drops the redundant data.operator.provenance.
    env = _handler(conn)(server="en", game_id=_AMIYA, include_provenance=False)
    op = env.to_dict()["data"]["operator"]  # type: ignore[index]
    assert "provenance" not in op
    assert env.to_dict()["provenance"][0]["server"] == "en"  # type: ignore[index]


def test_wrong_region_is_not_found(conn: sqlite3.Connection) -> None:
    # §V5: en data is not surfaced under a cn query -- en/cn never mixed.
    assert _handler(conn)(server="cn", game_id=_AMIYA).status == "not_found"


# --- §V23 typed failures ------------------------------------------------------


def test_not_found_envelope(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", game_id="char_999_ghost")
    assert env.status == "not_found"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert data["message"] == "no operator matched the given region and game_id"
    # §V24: a not_found never suggests a query-time download/scrape.
    assert "download" not in data["suggested_action"].lower()  # type: ignore[union-attr]
    assert "scrape" not in data["suggested_action"].lower()  # type: ignore[union-attr]


def test_database_unavailable_envelope() -> None:
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: cand.sqlite")

    env = build_get_operator_spec(boom).handler(server="en", game_id=_AMIYA)
    assert env.status == "database_unavailable"
    data = env.to_dict()["data"]
    # §V23: no local path / file name leaks into the client-facing message.
    assert data["message"] == "the active database is unavailable"  # type: ignore[index]
    assert "cand.sqlite" not in str(data)


def test_unexpected_error_fails_closed_to_internal_error() -> None:
    def boom() -> sqlite3.Connection:
        raise RuntimeError("secret path /home/ubuntu/db.sqlite blew up")

    env = build_get_operator_spec(boom).handler(server="en", game_id=_AMIYA)
    assert env.status == "internal_error"
    # §V23: the fixed message carries no exception text / stack trace / local path.
    assert str(env.to_dict()["data"]).find("/home/ubuntu") == -1
    assert "blew up" not in str(env.to_dict()["data"])


# --- §V18 input gate ----------------------------------------------------------


def test_unknown_parameter_rejected(conn: sqlite3.Connection) -> None:
    # §V18: extra="forbid" -> a crafted request cannot smuggle an unknown field.
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", game_id=_AMIYA, include_everything=True)


def test_missing_game_id_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(conn)(server="en")


def test_bad_region_rejected(conn: sqlite3.Connection) -> None:
    # §V5: server is constrained to en|cn.
    with pytest.raises(ValidationError):
        _handler(conn)(server="jp", game_id=_AMIYA)


def test_over_length_game_id_rejected(conn: sqlite3.Connection) -> None:
    # §V18: an over-length id cannot carry an oversized blob.
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", game_id="x" * (MAX_ID_LEN + 1))


# --- §V2 read-only / §I.tool wire contract ------------------------------------


def test_service_is_read_only(conn: sqlite3.Connection) -> None:
    # §V2: the service only reads -- no writes recorded on the connection.
    before = conn.total_changes
    get_operator(conn, server="en", game_id=_AMIYA, include_modules=True)
    assert conn.total_changes == before


def test_spec_registers_read_only_with_bounded_schema(conn: sqlite3.Connection) -> None:
    reg = ToolRegistry()
    spec = reg.register(build_get_operator_spec(lambda: conn))
    assert reg.names() == ("get_operator",)
    assert spec.read_only is True
    tool = spec.to_mcp_tool()
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True
    # §V18: unknown params forbidden + the game_id length cap rides the wire (§V5
    # requires server).
    assert tool.inputSchema["additionalProperties"] is False
    assert set(tool.inputSchema["required"]) == {"server", "game_id"}
    assert tool.inputSchema["properties"]["game_id"]["maxLength"] == MAX_ID_LEN
