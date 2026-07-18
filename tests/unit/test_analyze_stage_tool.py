"""§T40 ``analyze_stage`` tool tests (§V6/§V7/§V23; §V5/§V14; §I.tool).

The tool is the model -> service -> envelope bridge for a single stage's threat
analysis; these drive it end to end against the same production read-only path
(§V2) using the pinned 4-4 fixture. They assert:

* **§V6** -- every emitted observation carries all five mandated fields
  (``rule_id`` + evidence + confidence + limitations + ``analyzer_version``) at
  *every* depth, and the envelope stamps the analyzer version;
* **§V7** -- the tool returns facts + evidence-backed observations only; it emits
  no recommendations key and no "mandatory"/best-in-slot verdict;
* the ``depth`` ladder (summary / standard / detailed) scales the surrounding
  facts, not the observations: summary is observations-only, standard adds the
  compact enemy roster + warnings, detailed swaps in full per-enemy context;
* the §V5 region + provenance ride every ``ok`` result, en/cn never mixed;
* the typed §V23 envelope shape, including fail-closed ``not_found`` /
  ``database_unavailable`` / ``internal_error`` with no path/trace leak.
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
from arknights_mcp.mcp.tools.stage import build_analyze_stage_spec
from arknights_mcp.services.stages import analyze_stage
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Build the 4-4 fixture candidate read-only (stage + two enemies, one flyer)."""
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    return open_read_only(path)


def _handler(conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    return build_analyze_stage_spec(lambda: conn).handler


# --- depth ladder -------------------------------------------------------------


def test_standard_is_the_default_depth(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", stage_code="4-4")
    assert env.status == "ok"
    assert env.schema_version == SCHEMA_VERSION
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert data["depth"] == "standard"
    # standard: observations + enemy roster + warnings around the stage facts.
    assert set(data) == {"depth", "stage", "observations", "occurrences", "warnings"}


def test_summary_is_observations_only(conn: sqlite3.Connection) -> None:
    data = _handler(conn)(server="en", stage_code="4-4", depth="summary").to_dict()["data"]
    # summary drops the enemy roster + warnings; observations stay full (§V6).
    assert set(data) == {"depth", "stage", "observations"}  # type: ignore[arg-type]
    assert data["observations"]  # type: ignore[index]


def test_summary_keys_are_subset_of_standard(conn: sqlite3.Connection) -> None:
    handler = _handler(conn)
    summ = handler(server="en", stage_code="4-4", depth="summary").to_dict()["data"]
    std = handler(server="en", stage_code="4-4", depth="standard").to_dict()["data"]
    assert set(summ) < set(std)  # type: ignore[arg-type]


def test_standard_roster_is_compact(conn: sqlite3.Connection) -> None:
    data = _handler(conn)(server="en", stage_code="4-4", depth="standard").to_dict()["data"]
    roster = data["occurrences"]  # type: ignore[index]
    by_id = {o["game_id"]: o for o in roster}
    assert set(by_id) == {"enemy_1007_slime", "enemy_1105_drone"}
    # compact roster: identity + how many, no per-enemy stat/motion fields.
    assert set(by_id["enemy_1105_drone"]) == {
        "game_id",
        "display_name",
        "is_boss",
        "is_elite",
        "total_count",
    }


def test_detailed_roster_carries_full_typed_context(conn: sqlite3.Connection) -> None:
    data = _handler(conn)(server="en", stage_code="4-4", depth="detailed").to_dict()["data"]
    assert data["depth"] == "detailed"  # type: ignore[index]
    by_id = {o["game_id"]: o for o in data["occurrences"]}  # type: ignore[index]
    drone = by_id["enemy_1105_drone"]
    # detailed exposes the typed fields the compact roster omits.
    assert drone["motion_type"] == "FLY"
    assert drone["total_count"] == 2
    assert drone["first_spawn_time"] == 8.0
    assert {"enemy_class", "attack_type", "level_variant", "route_count"} <= set(drone)


# --- §V6 evidence-backed observations -----------------------------------------


def test_observations_carry_every_v6_field_at_all_depths(conn: sqlite3.Connection) -> None:
    handler = _handler(conn)
    for depth in ("summary", "standard", "detailed"):
        env = handler(server="en", stage_code="4-4", depth=depth)
        data = env.to_dict()["data"]
        observations = data["observations"]  # type: ignore[index]
        assert observations  # 4-4 fields a flyer -> at least the aerial observation
        for obs in observations:
            # §V6: every mandated field present + well-formed at every depth.
            assert obs["rule_id"]
            assert isinstance(obs["evidence"], list) and obs["evidence"]
            assert 0.0 <= obs["confidence"] <= 1.0
            assert isinstance(obs["limitations"], list)
            assert obs["analyzer_version"]
            for ev in obs["evidence"]:
                assert set(ev) == {"ref", "field", "value", "note"}
                assert ev["ref"] and ev["field"]


def test_aerial_observation_surfaced_with_evidence(conn: sqlite3.Connection) -> None:
    data = _handler(conn)(server="en", stage_code="4-4").to_dict()["data"]
    by_tag = {o["tag"]: o for o in data["observations"]}  # type: ignore[index]
    assert "aerial" in by_tag
    aerial = by_tag["aerial"]
    # Evidence traces to the flying drone only, from the typed motion field (§V6).
    refs = {e["ref"] for e in aerial["evidence"]}
    assert refs == {"enemy_1105_drone"}
    assert aerial["confidence"] >= 0.9  # authoritative motion_type=FLY


def test_envelope_stamps_analyzer_version(conn: sqlite3.Connection) -> None:
    # §V6: the analyzer version rides the envelope top-level, matching the per-obs one.
    env = _handler(conn)(server="en", stage_code="4-4")
    version = env.to_dict()["analyzer_version"]
    assert version
    obs = env.to_dict()["data"]["observations"]  # type: ignore[index]
    assert all(o["analyzer_version"] == version for o in obs)


# --- §V7 facts + observations only, no recommendation ------------------------


def test_no_recommendation_or_prescriptive_verdict(conn: sqlite3.Connection) -> None:
    # §V7: the tool emits facts + evidence-backed observations only -- no
    # recommendations key, and nothing labelled mandatory / best-in-slot.
    data = _handler(conn)(server="en", stage_code="4-4", depth="detailed").to_dict()["data"]
    assert "recommendations" not in data  # type: ignore[operator]
    blob = str(data).lower()
    for banned in ("mandatory", "best-in-slot", "must use", "required operator"):
        assert banned not in blob


# --- §V5 region + provenance --------------------------------------------------


def test_ok_carries_region_and_provenance(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", stage_code="4-4")
    prov = env.to_dict()["provenance"]
    assert isinstance(prov, list) and len(prov) == 1
    assert prov[0]["server"] == "en"
    assert prov[0]["snapshot_id"]
    assert prov[0]["imported_at"]


def test_lookup_by_game_id_matches_code(conn: sqlite3.Connection) -> None:
    by_code = _handler(conn)(server="en", stage_code="4-4").to_dict()
    by_id = _handler(conn)(server="en", game_id="main_04-04").to_dict()
    assert by_code == by_id


def test_wrong_region_is_not_found(conn: sqlite3.Connection) -> None:
    # §V5: en data is not surfaced under a cn query.
    assert _handler(conn)(server="cn", stage_code="4-4").status == "not_found"


# --- §V23 / §V5 typed failures ------------------------------------------------


def test_not_found_envelope(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", stage_code="9-9")
    assert env.status == "not_found"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert data["message"] == "no stage matched the given region and selector"
    # §V24: a not_found never suggests a query-time download/scrape.
    assert "download" not in data["suggested_action"].lower()  # type: ignore[union-attr]
    assert "scrape" not in data["suggested_action"].lower()  # type: ignore[union-attr]


def test_database_unavailable_envelope() -> None:
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: cand.sqlite")

    env = build_analyze_stage_spec(boom).handler(server="en", stage_code="4-4")
    assert env.status == "database_unavailable"
    data = env.to_dict()["data"]
    assert data["message"] == "the active database is unavailable"  # type: ignore[index]
    assert "cand.sqlite" not in str(data)


def test_unexpected_error_fails_closed_to_internal_error() -> None:
    def boom() -> sqlite3.Connection:
        raise RuntimeError("secret path /home/ubuntu/db.sqlite blew up")

    env = build_analyze_stage_spec(boom).handler(server="en", stage_code="4-4")
    assert env.status == "internal_error"
    # §V23: the fixed message carries no exception text / stack trace / local path.
    assert str(env.to_dict()["data"]).find("/home/ubuntu") == -1
    assert "blew up" not in str(env.to_dict()["data"])


# --- §V18/§V19 model gate -----------------------------------------------------


def test_bad_depth_rejected_at_gate(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", stage_code="4-4", depth="verbose")


def test_unknown_parameter_rejected(conn: sqlite3.Connection) -> None:
    # §V18: extra="forbid" -> a crafted request cannot smuggle an unknown field.
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", stage_code="4-4", include_map=True)


def test_selector_must_be_exactly_one(conn: sqlite3.Connection) -> None:
    handler = _handler(conn)
    with pytest.raises(ValidationError):
        handler(server="en")  # neither
    with pytest.raises(ValidationError):
        handler(server="en", stage_code="4-4", game_id="main_04-04")  # both


# --- §V2 read-only / §I.tool wire contract ------------------------------------


def test_service_is_read_only(conn: sqlite3.Connection) -> None:
    # §V2: the service only reads -- no writes recorded on the connection.
    before = conn.total_changes
    analyze_stage(conn, server="en", stage_code="4-4")
    assert conn.total_changes == before


def test_spec_registers_read_only_with_bounded_schema(conn: sqlite3.Connection) -> None:
    reg = ToolRegistry()
    spec = reg.register(build_analyze_stage_spec(lambda: conn))
    assert reg.names() == ("analyze_stage",)
    assert spec.read_only is True
    tool = spec.to_mcp_tool()
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True
    # §V18: unknown params forbidden; §V6 depth enum rides the wire.
    assert tool.inputSchema["additionalProperties"] is False
    depth_schema = tool.inputSchema["properties"]["depth"]
    assert set(depth_schema["enum"]) == {"summary", "standard", "detailed"}
