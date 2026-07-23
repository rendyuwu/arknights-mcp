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
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.support.items import seed_items

from arknights_mcp.db.connection import DatabaseUnavailable, open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.envelopes import SCHEMA_VERSION
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools._shared import (
    BLACKBOARD_KEY_GLOSSARY,
    BLACKBOARD_LIMITATION,
    COST_ITEM_NAME_LIMITATION,
)
from arknights_mcp.mcp.tools.operator import (
    _phase_to_dict,
    _skill_level_to_dict,
    build_get_operator_spec,
)
from arknights_mcp.models.common import MAX_ID_LEN
from arknights_mcp.services.operators import (
    OperatorPhaseFacts,
    SkillLevelFacts,
    get_operator,
)
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


#: Amiya's CX-1 module upgrade-cost item ids (per level) + a display name for each, so a
#: seeded build can resolve them (§T132/§V69). The operator fixture itself ships no items.
_MODULE_COST_NAMES = {"mat_1": "Orirock Cube", "mat_2": "Sugar", "mat_3": "Polyester Pack"}


@pytest.fixture
def conn_named_costs(tmp_path: Path) -> sqlite3.Connection:
    """Fixture build with the module upgrade-cost item names seeded (§T132/§V69)."""
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    seed_items(path, _MODULE_COST_NAMES, region="en")
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
    # §V22: heavy sections are opt-in -- absent by default. §V66/B64: the in-data
    # provenance echo is opt-in too (default off), so a default response carries no
    # data.operator.provenance -- the snapshot rides the envelope once.
    assert set(op) == {"server", "game_id", "display_name", "summary"}  # type: ignore[arg-type]
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
    # §T138/§V67/B63: the always-null ``valueStr`` key is omitted at emit.
    assert lv["level"] == 1 and lv["blackboard"] == [{"key": "charge", "value": 1.0}]
    assert op["talents"][0]["display_name"] == "Nervous Impulse"
    mod = op["modules"][0]
    assert mod["module_type"] == "CX-1"
    assert mod["levels"][0]["cost"] == [{"id": "mat_1", "count": 8, "type": "MATERIAL"}]


def test_summary_can_be_dropped(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", game_id=_AMIYA, include_summary=False)
    op = env.to_dict()["data"]["operator"]  # type: ignore[index]
    assert "summary" not in op


# --- §V67 null discipline: omit the always-optional range_id scalar -----------


def test_phase_range_id_omitted_when_absent() -> None:
    # §V67: ``range_id`` is an always-optional scalar -- omitted when the source carried
    # none, emitted when present (never an ambiguous null).
    phase = OperatorPhaseFacts(
        phase=0,
        max_level=50,
        max_hp=1000,
        atk=100,
        def_=50,
        res=0,
        redeploy_time=70,
        cost=10,
        block_count=1,
        attack_interval=1.0,
        range_id=None,
    )
    assert "range_id" not in _phase_to_dict(phase)
    assert _phase_to_dict(replace(phase, range_id="1-1"))["range_id"] == "1-1"


def test_skill_level_range_id_omitted_when_absent() -> None:
    level = SkillLevelFacts(
        level=1,
        sp_cost=10,
        initial_sp=0,
        duration=5.0,
        range_id=None,
        blackboard=None,
        description=None,
    )
    assert "range_id" not in _skill_level_to_dict(level)
    assert _skill_level_to_dict(replace(level, range_id="x-1"))["range_id"] == "x-1"


# --- §V65/T126: blackboard grounding FLOOR ------------------------------------


def test_blackboard_sections_carry_grounding_limitation(conn: sqlite3.Connection) -> None:
    # §V65 (b): skills/talents/modules emit raw blackboard key-value data with no
    # effect text, so any response that carries one of them attaches the standing
    # grounding limitation (client must not infer mechanics from key names).
    for flag in ("include_skills", "include_talents", "include_modules"):
        env = _handler(conn)(server="en", game_id=_AMIYA, **{flag: True})
        assert env.status == "ok"
        assert BLACKBOARD_LIMITATION in env.limitations, flag
        # §V26: the caveat is the executable "absent field -> say so" form.
        assert "do not infer" in BLACKBOARD_LIMITATION.lower()


def test_summary_only_response_has_no_blackboard_limitation(conn: sqlite3.Connection) -> None:
    # §V65: a summary-only response emits no blackboard, so it carries no such caveat.
    env = _handler(conn)(server="en", game_id=_AMIYA)
    assert BLACKBOARD_LIMITATION not in env.limitations


def test_effect_templates_ride_alongside_blackboard(conn: sqlite3.Connection) -> None:
    # §T127/§V65 (a)/ADR 0010: skill + talent + module effects emit the in-game effect
    # description template alongside the raw blackboard (additive/optional, §V21) so a
    # client can ground the key meanings instead of guessing.
    env = _handler(conn)(
        server="en",
        game_id=_AMIYA,
        include_skills=True,
        include_talents=True,
        include_modules=True,
    )
    op = env.to_dict()["data"]["operator"]  # type: ignore[index]
    skills = {s["game_id"]: s for s in op["skills"]}
    skill2 = skills["skchr_amiya_2"]
    lv = skill2["levels"][0]
    # §T138/§V67/B63: the always-null ``valueStr`` key is omitted at emit.
    assert lv["blackboard"] == [{"key": "atk", "value": 1.5}]
    # §T146/§V66.3: skchr_amiya_2 has one level, so its template is byte-identical across
    # "all" levels and is hoisted once to the skill; the level no longer carries it.
    assert "{atk:0%}" in skill2["description"]  # template references the blackboard key
    assert "description" not in lv
    tvar = op["talents"][0]["variants"][0]
    assert "{atk_scale:0%}" in tvar["description"] and tvar["blackboard"]
    # get_operator modules are unchanged by §T146: the trait change still carries its
    # template inline alongside its blackboard (level 1).
    trait = op["modules"][0]["levels"][0]["trait_changes"]
    assert trait and "{atk_scale:0%}" in trait[0]["description"]


def test_skill_template_kept_per_level_when_levels_differ(conn: sqlite3.Connection) -> None:
    # §T146/§V66.3: the hoist is byte-lossless. skchr_amiya_1's two levels carry DIFFERENT
    # templates (level 1 has a {charge} placeholder, level 2 does not), so nothing is
    # hoisted to the skill and each level keeps its own text -- no wording is lost.
    env = _handler(conn)(server="en", game_id=_AMIYA, include_skills=True)
    op = env.to_dict()["data"]["operator"]  # type: ignore[index]
    skill1 = {s["game_id"]: s for s in op["skills"]}["skchr_amiya_1"]
    assert "description" not in skill1  # not hoisted -- templates differ across levels
    levels = {lv["level"]: lv for lv in skill1["levels"]}
    assert "{charge}" in levels[1]["description"]
    assert "{charge}" not in levels[2]["description"]
    assert levels[1]["description"] != levels[2]["description"]


def test_description_carries_blackboard_glossary(conn: sqlite3.Connection) -> None:
    # §V65 (c): a common-key glossary rides the tool description so a client has a
    # grounded reference for the emitted keys instead of guessing.
    desc = build_get_operator_spec(lambda: conn).description
    assert BLACKBOARD_KEY_GLOSSARY in desc
    for key in ("atk_scale", "attack@times", "stun", "prob", "max_hp"):
        assert key in desc, key


def test_client_facing_blackboard_text_has_no_internal_cites() -> None:
    # §V71 (b): published client-facing text carries no internal spec cites or jargon;
    # the behavioral sentence stays, the cites live only in code/docs.
    for text in (BLACKBOARD_LIMITATION, BLACKBOARD_KEY_GLOSSARY):
        assert "§V" not in text and "§T" not in text
        assert "degenerate" not in text and "asymmetric-broken" not in text


# --- §T132/§V69 module upgrade-cost item name pairing -------------------------


def test_module_cost_items_paired_with_display_name(conn_named_costs: sqlite3.Connection) -> None:
    # §V69: each {id,count,type} upgrade-cost entry gains its item display_name when the
    # name is present in this build (additive, §V21).
    env = _handler(conn_named_costs)(server="en", game_id=_AMIYA, include_modules=True)
    assert env.status == "ok"
    module = env.to_dict()["data"]["operator"]["modules"][0]  # type: ignore[index]
    cost_by_level = {lv["level"]: lv["cost"] for lv in module["levels"]}
    assert cost_by_level[1] == [
        {"id": "mat_1", "count": 8, "type": "MATERIAL", "display_name": "Orirock Cube"}
    ]
    assert cost_by_level[2][0]["display_name"] == "Sugar"
    assert cost_by_level[3][0]["display_name"] == "Polyester Pack"
    # §V69: every cost item resolved, so no cost-name limitation rides the response.
    assert COST_ITEM_NAME_LIMITATION not in env.limitations


def test_module_cost_absent_name_keeps_id_and_emits_limitation(conn: sqlite3.Connection) -> None:
    # §V69/§V26: the fixture ships no items, so the cost item ids have no imported name ->
    # the id is emitted exactly as stored (never a fabricated name) + the standing
    # cost-name limitation rides the response.
    env = _handler(conn)(server="en", game_id=_AMIYA, include_modules=True)
    module = env.to_dict()["data"]["operator"]["modules"][0]  # type: ignore[index]
    for lv in module["levels"]:
        for entry in lv["cost"]:
            assert entry["id"]  # the bare id is preserved
            assert "display_name" not in entry  # never fabricated (§V26)
    assert COST_ITEM_NAME_LIMITATION in env.limitations


def test_cost_name_pairing_is_additive(conn_named_costs: sqlite3.Connection) -> None:
    # §V21: pairing preserves the original id/count/type keys (adds display_name only).
    env = _handler(conn_named_costs)(server="en", game_id=_AMIYA, include_modules=True)
    entry = env.to_dict()["data"]["operator"]["modules"][0]["levels"][0]["cost"][0]  # type: ignore[index]
    assert {"id", "count", "type"} <= set(entry)
    assert entry["id"] == "mat_1" and entry["count"] == 8 and entry["type"] == "MATERIAL"


def test_no_cost_name_limitation_without_modules(conn: sqlite3.Connection) -> None:
    # §V69: a response that does not include the modules section emits no cost at all, so
    # it carries no cost-name caveat.
    env = _handler(conn)(server="en", game_id=_AMIYA, include_skills=True)
    assert COST_ITEM_NAME_LIMITATION not in env.limitations


def test_cost_name_limitation_has_no_internal_cites() -> None:
    # §V71 (b): the client-facing limitation carries no internal spec cites or jargon.
    assert "§V" not in COST_ITEM_NAME_LIMITATION and "§T" not in COST_ITEM_NAME_LIMITATION


# --- §V5 region + provenance --------------------------------------------------


def test_ok_carries_region_and_provenance(conn: sqlite3.Connection) -> None:
    # §V5: every factual response carries region + provenance on the envelope.
    env = _handler(conn)(server="en", game_id=_AMIYA)
    prov = env.to_dict()["provenance"]
    assert isinstance(prov, list) and len(prov) == 1
    assert prov[0]["server"] == "en"
    assert prov[0]["snapshot_id"].startswith("en:")
    assert prov[0]["imported_at"]


def test_default_response_carries_provenance_exactly_once(conn: sqlite3.Connection) -> None:
    # §V66/B64: the envelope is the SOLE default provenance carrier -- the in-data echo
    # is opt-in (default off), so a default response carries the snapshot exactly once
    # (on the envelope) rather than duplicating it inside data.operator.
    out = _handler(conn)(server="en", game_id=_AMIYA).to_dict()
    assert len(out["provenance"]) == 1  # type: ignore[arg-type]
    assert "provenance" not in out["data"]["operator"]  # type: ignore[index]


def test_include_provenance_true_adds_the_in_data_echo(conn: sqlite3.Connection) -> None:
    # §V21: the echo stays available as an opt-in extra; when requested it mirrors the
    # envelope snapshot inside data.operator (the flag is now fully effective, B64).
    out = _handler(conn)(server="en", game_id=_AMIYA, include_provenance=True).to_dict()
    op = out["data"]["operator"]  # type: ignore[index]
    assert op["provenance"]["snapshot_id"] == out["provenance"][0]["snapshot_id"]  # type: ignore[index]


def test_include_provenance_false_keeps_envelope_provenance(conn: sqlite3.Connection) -> None:
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
