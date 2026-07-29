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
from arknights_mcp.mcp.tools._shared import ENEMY_STAT_SCALE_NOTE, LIST_FIELD_CONVENTION
from arknights_mcp.mcp.tools.enemy import (
    _enemy_absent_field_limitations,
    _enemy_to_dict,
    build_get_enemy_spec,
)
from arknights_mcp.models.common import MAX_ID_LEN
from arknights_mcp.services.enemies import (
    EnemyFacts,
    EnemyLevelFacts,
    EnemyProvenance,
    get_enemy,
)
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
    # §V104 (b)/§T207: every enemy row carries enemy_class + motion_type, and §T210 added
    # three more typed enums (damage_types / targeting / immunities), so their static
    # domains ride the response beside the values instead of the tool description.
    assert set(data) == {"enemy", "enum_legend"}
    assert set(data["enum_legend"]) == {  # type: ignore[arg-type,index]
        "enemy_class",
        "motion_type",
        "damage_types",
        "targeting",
        "immunities",
    }
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
    assert lvl["targeting"] == "MELEE"
    # Structural JSON is decoded back to a Python object (§V18 vetted at import).
    assert lvl["immunities"] == []


def test_aerial_enemy_immunities_decoded(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", game_id="enemy_1105_drone")
    enemy = env.to_dict()["data"]["enemy"]  # type: ignore[index]
    assert enemy["is_elite"] is True
    assert enemy["motion_type"] == "FLY"
    assert enemy["damage_types"] == ["MAGIC"]
    lvl = enemy["levels"][0]  # type: ignore[index]
    assert lvl["res"] == 10
    # §T210: nine typed upstream flags fold into one list; only the set ones are named.
    assert lvl["immunities"] == ["SILENCE"]


# --- §V67/§V26 (B58) null discipline: [] vs absent + absent-field limitation ---


def test_absent_list_fields_are_omitted_not_null(conn: sqlite3.Connection) -> None:
    # §V67: the 4-4 slug confirms no immunities ([]), but no source carries abilities or
    # block_behavior at all -> those keys are OMITTED, never emitted as null, so a client
    # can tell "confirmed none" ([]) apart from "not in source" (absent, B58).
    env = _handler(conn)(server="en", game_id="enemy_1007_slime")
    lvl = env.to_dict()["data"]["enemy"]["levels"][0]  # type: ignore[index]
    assert lvl["immunities"] == []  # confirmed none, present
    assert "abilities" not in lvl  # not in source -> omitted
    assert "block_behavior" not in lvl  # not in source -> omitted


def test_dead_by_data_fields_named_with_their_true_scope(conn: sqlite3.Connection) -> None:
    # §V113/§V26 (B160 c): block_behavior + abilities are absent from EVERY enemy on
    # every build because no source carries them, so the limitation says so instead of
    # the per-entity "this entity's source data" phrasing, which would imply some other
    # enemy has them (§V108). It also explains why an analysis never reports those
    # threats -- the three rules that read these fields were retired.
    env = _handler(conn)(server="en", game_id="enemy_1007_slime")
    assert env.status == "ok"
    blob = " ".join(env.limitations).lower()
    assert "block_behavior" in blob and "abilities" in blob
    assert "for any enemy" in blob
    assert "block-bypass" in blob and "crowd-control" in blob and "support-aura" in blob


def test_retired_attack_type_routes_to_damage_types(conn: sqlite3.Connection) -> None:
    # §V113/§V108 (B160 b): the handbook still HAS attackType and upstream fills it on
    # 0/1585 entries, so its absence is a fact about the game data, not this enemy. The
    # limitation states that scope and names the field that answers the question.
    env = _handler(conn)(server="en", game_id="enemy_1007_slime")
    blob = " ".join(env.limitations)
    assert "no longer fills the older attack_type field for any enemy" in blob
    assert "damage_types states an enemy's damage kind" in blob
    # ...and it is NOT listed as a per-entity absent field, which would double-report it.
    assert "not present in this entity's source data: attack_type" not in blob
    # §V71: the client-facing limitation carries no internal spec cite/jargon.
    assert all("§v" not in lim.lower() and "b58" not in lim.lower() for lim in env.limitations)


def test_present_scalars_still_emitted_on_the_wire(conn: sqlite3.Connection) -> None:
    # §V67/B98 (T180): the omit rule touches ABSENT scalars only -- the fixture slug
    # carries damage_types/attack_range/targeting, so all three emit unchanged (§V21; a
    # genuine 0.0 is a present value, never dropped -- 25 real level rows store exactly
    # that radius) and none is flagged as an absent field.
    env = _handler(conn)(server="en", game_id="enemy_1007_slime")
    enemy = env.to_dict()["data"]["enemy"]  # type: ignore[index]
    lvl = enemy["levels"][0]  # type: ignore[index]
    assert enemy["damage_types"] == ["PHYSIC"]
    assert lvl["attack_range"] == 0.0  # present zero survives the omit rule
    assert lvl["targeting"] == "MELEE"
    # Nothing this enemy carries is reported as an entity-level gap: the only absence
    # note it earns is the corpus-wide dead-field one.
    assert not [lim for lim in env.limitations if "not present in this entity" in lim]


def test_bare_enemy_omits_absent_scalars_and_names_them() -> None:
    # §V67/B98 (T180): an enemy whose source omits damage_types + every per-level
    # attack_range/targeting emits NONE of those keys; the absent-field limitation
    # names all three (sole signal, no null+limitation duplicate).
    lvl = EnemyLevelFacts(
        level_variant=0,
        hp=100,
        atk=10,
        def_=0,
        res=0,
        attack_interval=None,
        attack_range=None,
        move_speed=None,
        weight=None,
        life_point_reduction=None,
        block_behavior=None,
        targeting=None,
        immunities=None,
        abilities=None,
    )
    enemy = EnemyFacts(
        server="en",
        game_id="enemy_bare",
        display_name="Bare",
        enemy_class="NORMAL",
        is_boss=False,
        is_elite=False,
        attack_type=None,
        damage_types=None,
        motion_type="WALK",
        levels=(lvl,),
        provenance=EnemyProvenance(snapshot_id="en:x", imported_at="t"),
    )
    data = _enemy_to_dict(enemy, image_refs_enabled=False)
    assert "attack_type" not in data and "damage_types" not in data
    level = data["levels"][0]  # type: ignore[index]
    assert "attack_range" not in level and "targeting" not in level
    absent = next(
        lim for lim in _enemy_absent_field_limitations(enemy) if "not present in this entity" in lim
    )
    blob = absent.lower()
    assert "damage_types" in blob and "attack_range" in blob and "targeting" in blob


def test_description_states_list_field_convention(conn: sqlite3.Connection) -> None:
    # §V67: the []-vs-absent + absent-scalar convention is stated in the description.
    assert LIST_FIELD_CONVENTION in build_get_enemy_spec(lambda: conn).description
    assert "scalar" in LIST_FIELD_CONVENTION


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


def test_stat_scales_ride_the_response_that_carries_the_stats(
    conn: sqlite3.Connection,
) -> None:
    # §V104/§V71 (e): res / move_speed / weight sit on the same block as attack_interval
    # ("in seconds") and stated nothing, so "res: 0" could be a percentage or a flat
    # value. §T207/§V111 (a) moved the home from this description to a standing
    # limitation -- a scale is read beside its number, and every stat block emits one.
    env = _handler(conn)(server="en", game_id="enemy_1007_slime")
    assert ENEMY_STAT_SCALE_NOTE in env.to_dict()["limitations"]
    assert ENEMY_STAT_SCALE_NOTE not in build_get_enemy_spec(lambda: conn).description
