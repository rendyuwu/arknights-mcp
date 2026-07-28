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
from arknights_mcp.mcp.tools._shared import (
    CONFIDENCE_SCALE_NOTE,
    ENEMY_STAT_SCALE_NOTE,
    LIST_FIELD_CONVENTION,
)
from arknights_mcp.mcp.tools.stage import (
    _occurrence_full,
    _shape_analysis,
    build_analyze_stage_spec,
)
from arknights_mcp.services.stages import (
    EnemyOccurrenceFacts,
    StageAnalysisResult,
    StageFacts,
    StageProvenance,
    analyze_stage,
)
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


def test_detailed_occurrence_carries_the_promised_stat_block(conn: sqlite3.Connection) -> None:
    # §V47 (B41): the detailed occurrence must emit the per-enemy stat block its tool
    # description promises ("full per-enemy stat/timing context") -- not just identity +
    # timing. The integration test pins the contract so a desc ⊃ output regression trips.
    data = _handler(conn)(server="en", stage_code="4-4", depth="detailed").to_dict()["data"]
    stat_keys = {"hp", "atk", "def", "res", "attack_interval", "move_speed", "weight"}
    for occ in data["occurrences"]:  # type: ignore[index]
        assert stat_keys <= set(occ), f"detailed occurrence missing stats: {occ['game_id']}"
    by_id = {o["game_id"]: o for o in data["occurrences"]}  # type: ignore[index]
    # Concrete typed values from the pinned fixture (level 0), not just key presence.
    drone = by_id["enemy_1105_drone"]
    assert drone["hp"] == 900
    assert drone["atk"] == 260
    assert drone["def"] == 0
    assert drone["res"] == 10
    assert drone["attack_interval"] == 1.6
    assert drone["move_speed"] == 1.0
    assert drone["weight"] == 1
    # The stat block is what detailed adds over standard: compact roster omits them.
    compact = _handler(conn)(server="en", stage_code="4-4", depth="standard").to_dict()["data"]
    for occ in compact["occurrences"]:  # type: ignore[index]
        assert not (stat_keys & set(occ))


def _occurrence(attack_type: str | None) -> EnemyOccurrenceFacts:
    return EnemyOccurrenceFacts(
        game_id="enemy_bare",
        display_name="Bare",
        enemy_class="NORMAL",
        is_boss=False,
        is_elite=False,
        motion_type="WALK",
        attack_type=attack_type,
        level_variant=0,
        total_count=1,
        first_spawn_time=None,
        last_spawn_time=None,
        route_count=None,
        hp=100,
        atk=10,
        def_=0,
        res=0,
        attack_interval=None,
        move_speed=None,
        weight=None,
        variant_id=None,
    )


def test_detailed_occurrence_omits_absent_attack_type() -> None:
    # §V67/B98 (T180): an absent-in-source attack_type is OMITTED from the detailed
    # occurrence, never emitted as null; a present one still emits (§V21).
    assert "attack_type" not in _occurrence_full(_occurrence(None))
    assert _occurrence_full(_occurrence("physical"))["attack_type"] == "physical"


def test_detailed_envelope_names_absent_occurrence_attack_type() -> None:
    # §V67/B98 follow-through (review-fix): when a detailed occurrence omits an
    # absent-in-source attack_type, the envelope must NAME the omission (limitation
    # = sole signal, mirroring get_enemy) -- never a silent key drop. standard depth
    # emits no per-occurrence attack_type at all, so it carries no such caveat.
    stage = StageFacts(
        server="en",
        game_id="bare_stage",
        stage_code="B-1",
        display_name="Bare",
        zone_game_id=None,
        zone_display_name=None,
        event_name=None,
        stage_type=None,
        difficulty=None,
        sanity_cost=10,
        recommended_level=45,
        max_life_points=3,
        provenance=StageProvenance(snapshot_id="en:x", imported_at="t"),
    )
    result = StageAnalysisResult(
        status="ok",
        server="en",
        stage=stage,
        occurrences=(_occurrence(None), _occurrence("physical")),
        observations=(),
        warnings=(),
        analyzer_version="test",
    )
    detailed = _shape_analysis("detailed", result)
    assert any("attack_type" in lim.lower() for lim in detailed.limitations)
    standard = _shape_analysis("standard", result)
    assert not any("attack_type" in lim.lower() for lim in standard.limitations)


def test_analyze_description_states_field_convention() -> None:
    # §V67 "convention stated in tool descriptions": analyze_stage omits absent
    # scalars (attack_type / variant_id / recommended_level / max_life_points), so
    # its description carries the same shared convention get_stage/get_enemy state.
    conn = sqlite3.connect(":memory:")
    assert LIST_FIELD_CONVENTION in build_analyze_stage_spec(lambda: conn).description


def test_analysis_envelope_names_absent_stage_scalars() -> None:
    # §V67/B98 (T180): analyze_stage shares the stage shaper with get_stage, so a
    # stage whose source omits recommended_level/max_life_points drops the keys AND
    # carries the same sole-signal limitation naming them.
    bare = StageFacts(
        server="en",
        game_id="bare_stage",
        stage_code="B-1",
        display_name="Bare",
        zone_game_id=None,
        zone_display_name=None,
        event_name=None,
        stage_type=None,
        difficulty=None,
        sanity_cost=10,
        recommended_level=None,
        max_life_points=None,
        provenance=StageProvenance(snapshot_id="en:x", imported_at="t"),
    )
    result = StageAnalysisResult(
        status="ok",
        server="en",
        stage=bare,
        occurrences=(),
        observations=(),
        warnings=(),
        analyzer_version="test",
    )
    env = _shape_analysis("standard", result)
    assert env.status == "ok"
    stage = env.to_dict()["data"]["stage"]  # type: ignore[index]
    assert "recommended_level" not in stage and "max_life_points" not in stage
    blob = " ".join(env.limitations).lower()
    assert "recommended_level" in blob and "max_life_points" in blob
    assert "not present" in blob


def test_detailed_occurrence_omits_variant_id_for_base_enemy(
    conn: sqlite3.Connection,
) -> None:
    # §V67/B90: the 4-4 occurrences are base enemies (no inline useDb:false variant),
    # so a detailed occurrence omits ``variant_id`` rather than emitting a bare null.
    data = _handler(conn)(server="en", stage_code="4-4", depth="detailed").to_dict()["data"]
    for occ in data["occurrences"]:  # type: ignore[index]
        assert "variant_id" not in occ


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


def test_enum_legend_and_stat_scales_ride_only_the_detailed_depth(
    conn: sqlite3.Connection,
) -> None:
    # §V104 (b)/§V67: only the DETAILED occurrence row carries enemy_class and the
    # res/move_speed/weight block, so only that depth ships their domain + scales. A
    # legend or a scale for a field this depth never emits is noise (§V66).
    detailed = _handler(conn)(server="en", stage_code="4-4", depth="detailed").to_dict()
    assert set(detailed["data"]["enum_legend"]) == {"enemy_class"}  # type: ignore[arg-type,index]
    assert ENEMY_STAT_SCALE_NOTE in detailed["limitations"]
    for depth in ("summary", "standard"):
        env = _handler(conn)(server="en", stage_code="4-4", depth=depth).to_dict()
        assert "enum_legend" not in env["data"], depth  # type: ignore[operator]
        assert ENEMY_STAT_SCALE_NOTE not in env["limitations"], depth


def test_confidence_scale_rides_every_observation_bearing_depth(
    conn: sqlite3.Connection,
) -> None:
    # §V104/§V6: observations ride EVERY depth (the depth ladder scales the surrounding
    # facts, not the observations), so the scale their confidence is read on rides every
    # depth too -- once per envelope (§V66), not once per observation.
    for depth in ("summary", "standard", "detailed"):
        env = _handler(conn)(server="en", stage_code="4-4", depth=depth).to_dict()
        assert env["data"]["observations"], depth  # type: ignore[index]
        assert env["limitations"].count(CONFIDENCE_SCALE_NOTE) == 1, depth  # type: ignore[union-attr]
