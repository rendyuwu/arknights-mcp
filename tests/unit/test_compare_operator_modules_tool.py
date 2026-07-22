"""§T45 ``compare_operator_modules`` tool tests (§V5/§V7/§V23; §I.tool).

The tool is the model -> service -> envelope bridge for comparing one operator's
modules across module levels; these drive it end to end against the production
read-only path (§V2) using the operator fixture (Amiya: one CX-1 module with three
real levels -- atk 34/48/66, +150 Max HP at level 3, a trait change at level 1 and
a talent change at level 2). They assert:

* the §V5 region + provenance ride every ``ok`` result, and an ``en`` operator is
  never surfaced under a ``cn`` query;
* ``facts_only`` returns the per-level comparison; the requested ``levels`` subset
  is honored; ``with_observations`` adds §V6 observations + the analyzer version;
* §V7: observations state capability facts, never a mandatory/best-in-slot verdict;
* the typed §V23 envelope, including fail-closed ``not_found`` /
  ``database_unavailable`` / ``internal_error`` with no path/trace leak;
* the §I.tool wire contract: a read-only spec with a bounded input schema, and the
  §V18/§V19 input gate (unknown param, over-length id, bad region, bad levels).
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
from arknights_mcp.mcp.tools._shared import BLACKBOARD_KEY_GLOSSARY, BLACKBOARD_LIMITATION
from arknights_mcp.mcp.tools.module_compare import build_compare_operator_modules_spec
from arknights_mcp.models.common import MAX_ID_LEN
from arknights_mcp.services.module_compare import compare_operator_modules
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "operator" / "en"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

_AMIYA = "char_002_amiya"
_PRESCRIPTIVE = ("mandatory", "best-in-slot", "best in slot", "must ", "should ", "always use")


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
    return build_compare_operator_modules_spec(lambda: conn).handler


def _cx1(data: dict) -> dict:  # type: ignore[type-arg]
    modules = data["modules"]
    assert len(modules) == 1
    return modules[0]  # type: ignore[no-any-return]


# --- facts_only: the per-level comparison -------------------------------------


def test_facts_only_compares_all_three_levels(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", game_id=_AMIYA)
    assert env.status == "ok"
    assert env.schema_version == SCHEMA_VERSION
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert data["mode"] == "facts_only"
    assert data["levels"] == [1, 2, 3]
    # facts_only carries no observations section.
    assert "observations" not in data and "warnings" not in data
    assert env.to_dict()["analyzer_version"] is None
    module = _cx1(data)
    assert module["module_type"] == "CX-1"
    levels = {lv["level"]: lv for lv in module["levels"]}
    assert all(levels[n]["present"] for n in (1, 2, 3))
    # The atk progression + the level-3 max_hp bonus survive as typed structural JSON.
    assert levels[1]["stat_bonus"] == [{"key": "atk", "value": 34, "valueStr": None}]
    assert {"key": "max_hp", "value": 150} in levels[3]["stat_bonus"]
    assert levels[1]["cost"] == [{"id": "mat_1", "count": 8, "type": "MATERIAL"}]


def test_levels_subset_is_honored(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", game_id=_AMIYA, levels=[3, 1])
    data = env.to_dict()["data"]
    assert data["levels"] == [1, 3]  # deduped + sorted
    assert [lv["level"] for lv in _cx1(data)["levels"]] == [1, 3]


# --- with_observations: §V6 observations + §V7 conservative -------------------


def test_with_observations_emits_module_observations(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", game_id=_AMIYA, mode="with_observations")
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert data["mode"] == "with_observations"
    obs = data["observations"]
    assert isinstance(obs, list) and obs
    tags = {o["tag"] for o in obs}
    assert {"stat_bonus", "trait_change", "talent_change"} <= tags
    # §V6: every observation carries the five fields; the version rides the envelope.
    version = env.to_dict()["analyzer_version"]
    assert version is not None
    for o in obs:
        assert o["rule_id"] and o["evidence"] and 0.0 <= o["confidence"] <= 1.0
        assert isinstance(o["limitations"], list)
        assert o["analyzer_version"] == version
    assert "warnings" in data


def test_observations_are_conservative(conn: sqlite3.Connection) -> None:
    # §V7: capability facts only -- never a mandatory/best-in-slot recommendation.
    env = _handler(conn)(server="en", game_id=_AMIYA, mode="with_observations")
    blob = str(env.to_dict()["data"]["observations"]).lower()  # type: ignore[index]
    assert not any(word in blob for word in _PRESCRIPTIVE)


# --- §V65/T126: blackboard grounding FLOOR ------------------------------------


def test_module_changes_carry_grounding_limitation(conn: sqlite3.Connection) -> None:
    # §V65 (b): the per-level stat/trait/talent changes are raw blackboard key-value
    # data with no effect text, so every comparison that emits a module attaches the
    # standing grounding limitation -- in BOTH modes (facts_only also emits the raw
    # blackboard).
    for mode in ("facts_only", "with_observations"):
        env = _handler(conn)(server="en", game_id=_AMIYA, mode=mode)
        assert env.status == "ok"
        assert _cx1(env.to_dict()["data"])  # the fixture module is emitted
        assert BLACKBOARD_LIMITATION in env.limitations, mode


def test_effect_template_rides_trait_and_talent_changes(conn: sqlite3.Connection) -> None:
    # §T127/§V65 (a)/ADR 0010: each per-level trait/talent change emits its in-game
    # effect description template alongside the raw blackboard (additive, §V21).
    env = _handler(conn)(server="en", game_id=_AMIYA)
    module = _cx1(env.to_dict()["data"])
    levels = {lv["level"]: lv for lv in module["levels"]}
    trait = levels[1]["trait_changes"]
    assert trait and "{atk_scale:0%}" in trait[0]["description"]
    talent = levels[2]["talent_changes"]
    assert talent and "{prob:0%}" in talent[0]["description"]


def test_description_carries_blackboard_glossary(conn: sqlite3.Connection) -> None:
    # §V65 (c): a common-key glossary rides the tool description so a client has a
    # grounded reference for the emitted keys instead of guessing.
    desc = build_compare_operator_modules_spec(lambda: conn).description
    assert BLACKBOARD_KEY_GLOSSARY in desc
    for key in ("atk_scale", "attack@times", "stun", "prob", "max_hp"):
        assert key in desc, key


def test_client_facing_blackboard_text_has_no_internal_cites() -> None:
    # §V71 (b): published client-facing text carries no internal spec cites or jargon.
    for text in (BLACKBOARD_LIMITATION, BLACKBOARD_KEY_GLOSSARY):
        assert "§V" not in text and "§T" not in text
        assert "degenerate" not in text and "asymmetric-broken" not in text


# --- §V5 region + provenance --------------------------------------------------


def test_ok_carries_region_and_provenance(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", game_id=_AMIYA)
    prov = env.to_dict()["provenance"]
    assert isinstance(prov, list) and len(prov) == 1
    assert prov[0]["server"] == "en"
    assert prov[0]["snapshot_id"].startswith("en:")
    assert prov[0]["imported_at"]


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
    action = str(data["suggested_action"]).lower()  # type: ignore[index]
    assert "download" not in action and "scrape" not in action


def test_database_unavailable_envelope() -> None:
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: cand.sqlite")

    env = build_compare_operator_modules_spec(boom).handler(server="en", game_id=_AMIYA)
    assert env.status == "database_unavailable"
    data = env.to_dict()["data"]
    assert data["message"] == "the active database is unavailable"  # type: ignore[index]
    assert "cand.sqlite" not in str(data)


def test_unexpected_error_fails_closed_to_internal_error() -> None:
    def boom() -> sqlite3.Connection:
        raise RuntimeError("secret path /home/ubuntu/db.sqlite blew up")

    env = build_compare_operator_modules_spec(boom).handler(server="en", game_id=_AMIYA)
    assert env.status == "internal_error"
    # §V23: the fixed message carries no exception text / stack trace / local path.
    assert str(env.to_dict()["data"]).find("/home/ubuntu") == -1
    assert "blew up" not in str(env.to_dict()["data"])


# --- §V18/§V19 input gate -----------------------------------------------------


def test_unknown_parameter_rejected(conn: sqlite3.Connection) -> None:
    # §V18: extra="forbid" -> a crafted request cannot smuggle an unknown field.
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", game_id=_AMIYA, include_everything=True)


def test_missing_game_id_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(conn)(server="en")


def test_bad_region_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(conn)(server="jp", game_id=_AMIYA)


def test_over_length_game_id_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", game_id="x" * (MAX_ID_LEN + 1))


def test_empty_levels_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", game_id=_AMIYA, levels=[])


def test_out_of_range_level_rejected(conn: sqlite3.Connection) -> None:
    # §T45: module levels are bounded to {1, 2, 3}; 4 is rejected.
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", game_id=_AMIYA, levels=[4])


def test_bad_mode_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", game_id=_AMIYA, mode="freeform")


# --- §V2 read-only / §I.tool wire contract ------------------------------------


def test_service_is_read_only(conn: sqlite3.Connection) -> None:
    before = conn.total_changes
    compare_operator_modules(conn, server="en", game_id=_AMIYA, mode="with_observations")
    assert conn.total_changes == before


def test_description_names_module_levels_not_potential(conn: sqlite3.Connection) -> None:
    # §V48/B40: the comparison axis is MODULE levels (1/2/3), NOT operator potential.
    # A client reads the tool description + input schema literally; calling the axis
    # "potential levels" conflates the module upgrade tier with the separate,
    # potential-gated talent axis (§T45) -> wrong tool selection. Pin the wording.
    spec = build_compare_operator_modules_spec(lambda: conn)
    desc = spec.description.lower()
    assert "module levels" in desc
    assert "potential" not in desc
    # The bounded input schema is client-facing (§V21): its description (the model
    # docstring) must not mislabel the axis either.
    schema_desc = str(spec.to_mcp_tool().inputSchema.get("description", "")).lower()
    assert "potential" not in schema_desc


def test_spec_registers_read_only_with_bounded_schema(conn: sqlite3.Connection) -> None:
    reg = ToolRegistry()
    spec = reg.register(build_compare_operator_modules_spec(lambda: conn))
    assert reg.names() == ("compare_operator_modules",)
    assert spec.read_only is True
    tool = spec.to_mcp_tool()
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True
    assert tool.inputSchema["additionalProperties"] is False
    assert set(tool.inputSchema["required"]) == {"server", "game_id"}
    assert tool.inputSchema["properties"]["game_id"]["maxLength"] == MAX_ID_LEN
