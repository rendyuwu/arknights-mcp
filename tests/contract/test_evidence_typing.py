"""T197: §V101's guard -- every evidence row is a typed tuple that RESOLVES.

§V6 says an observation must carry evidence; §V101 says what one row IS (B137). Three
of those four rules are only checkable against a real response, because they are claims
ABOUT the response:

* ``field`` must be a REAL emitted field path. A hand-written list of blessed field
  names cannot check that -- it just moves the guess. So this walks the SERIALIZED
  envelope and resolves each path against the record its ``ref`` names: the path is real
  iff the key is actually there. That is what caught ``talent_changes.token_effect``, a
  path the module analyzer had emitted since §T148 and no response has ever carried.
* ``value`` must be that field's scalar -- never a packed ``"def=200,res=50"`` string.
* ``note`` is prose. A number a client may USE is a fact with a path of its own, so it
  gets its own row. The one carve-out is the §V85 level list a deduped module row
  carries (``"module level 2; module level 3"``): ``count`` is dedup multiplicity and
  the §V101 tuple has no level slot, so prose is the only home the spec leaves it.

And §V68/B136: a stage-level row refs the stage's ``game_id``. ``stage_code`` is shared
by the normal/tough/challenge variants of one stage, so a row reffing ``"14-18"`` is
undecidable and un-joinable to the stage block, which is keyed on ``game_id``. B57 fixed
this on ``get_item_drops`` in §T131; the stage-analysis surface kept it, on BOTH of its
stage-level rules -- B136 named only ``lane_route``, and ``tiles_deploy`` had it too.

Two arms, because neither alone is enough:

* the FIXTURE arm runs offline in CI and covers the shapes the fixtures exercise;
* the REAL-BUILD arm covers the corpus the fixtures cannot. Counted at
  ``2026-07-28T170428Z`` BEFORE the fix, over 3264 en stages: 14679 lane_route rows
  (2453 of them reffing a stage_code), 3096 def_res_skew rows -- 100% of them the packed
  ``field="def/res"`` shape -- 2078 pressure_spike rows with two numbers buried in one
  note, 872 aerial rows carrying ``"total_count=7"``, and 482 tiles_deploy rows reffing a
  stage_code at a path (``buildable_ranged``) no response emits anywhere.

Note what this canNOT cover: ``attack_range``, ``block_behavior`` and ``abilities`` are
empty on every row of the real build (0/4343 ``enemy_levels``), so the block-bypass,
ranged-arts, crowd-control and support-aura rules emit nothing there. Their rows are
checked by the fixture arm only -- see the unit suites.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.tools.module_compare import build_compare_operator_modules_spec
from arknights_mcp.mcp.tools.stage import build_analyze_stage_spec
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
REGISTRY_PATH = REPO_ROOT / "config" / "data_sources.toml"
MANIFEST = REPO_ROOT / "data" / "current.json"

#: The §V85 level list a deduped module row carries -- the ONLY note allowed to hold a
#: number, and only in this exact shape, so a new packed note cannot hide behind it.
_LEVEL_NOTE = re.compile(r"^module level \d+( to \d+)?(; module level \d+( to \d+)?)*$")

#: A ``key=value`` pair anywhere in a note is the B137 shape itself (``total_count=43``).
_PACKED_NOTE = re.compile(r"\w+\s*=\s*\S")


def _active_build() -> Path | None:
    if not MANIFEST.is_file():
        return None
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    filename = manifest.get("database_filename")
    if not filename:
        return None
    path = REPO_ROOT / "data" / "builds" / str(filename)
    return path if path.is_file() else None


BUILD = _active_build()


@pytest.fixture(scope="module")
def fixture_conn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    """The golden en+cn fixture corpus, built through the production pipeline."""
    path = tmp_path_factory.mktemp("evidence_typing") / "cand.sqlite"
    build_candidate(
        path,
        [
            ServerImport(
                "en",
                LocalSnapshotAdapter(FIXTURES / "golden" / "en", "en", "local_snapshot"),
                "local_snapshot",
            ),
            ServerImport(
                "cn",
                LocalSnapshotAdapter(FIXTURES / "golden" / "cn", "cn", "local_snapshot"),
                "local_snapshot",
            ),
        ],
        registry=load_source_registry(REGISTRY_PATH),
        imported_at="2026-07-18T00:00:00+00:00",
    )
    conn = open_read_only(path)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def operator_conn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    """The operator fixture corpus -- the golden stage corpus carries no module."""
    path = tmp_path_factory.mktemp("evidence_typing_ops") / "cand.sqlite"
    build_candidate(
        path,
        [
            ServerImport(
                "en",
                LocalSnapshotAdapter(FIXTURES / "operator" / "en", "en", "local_snapshot"),
                "local_snapshot",
            )
        ],
        registry=load_source_registry(REGISTRY_PATH),
        imported_at="2026-07-18T00:00:00+00:00",
    )
    conn = open_read_only(path)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def build_conn() -> Iterator[sqlite3.Connection]:
    if BUILD is None:
        pytest.skip("needs a promoted build (data/current.json); run `arknights-mcp sync` first")
    conn = sqlite3.connect(f"file:{BUILD}?mode=ro", uri=True)
    yield conn
    conn.close()


# --- the resolver -------------------------------------------------------------------


def _resolve(record: Any, path: str) -> bool:
    """True iff ``path`` names something the emitted ``record`` really carries.

    Walks dotted segments. A list is satisfied when ANY entry carries the next segment
    (``talent_changes.talentIndex`` resolves against the list of talent-change objects),
    and a ``{key, value}`` pair list -- how ``stat_bonus`` is emitted -- is satisfied when
    some entry's ``key`` equals the segment, which is what makes §V101's own blessed
    ``stat_bonus.atk`` spelling resolvable. A trailing ``count`` resolves against a list,
    naming how many entries it holds.
    """
    cursor: Any = record
    segments = path.split(".")
    for index, segment in enumerate(segments):
        if isinstance(cursor, dict):
            if segment not in cursor:
                return False
            cursor = cursor[segment]
            continue
        if isinstance(cursor, list):
            if segment == "count" and index == len(segments) - 1:
                return True
            matched = [
                entry[segment] for entry in cursor if isinstance(entry, dict) and segment in entry
            ]
            if matched:
                cursor = matched[0]
                continue
            # A {key, value} pair list: the segment names one pair's key.
            return any(isinstance(entry, dict) and entry.get("key") == segment for entry in cursor)
        return False
    return True


def _stage_records(data: dict[str, Any]) -> dict[str, Any]:
    """``ref`` -> the emitted record it names, for an ``analyze_stage`` response."""
    stage = data["stage"]
    records: dict[str, Any] = {stage["game_id"]: stage}
    for occurrence in data.get("occurrences", []):
        records[occurrence["game_id"]] = occurrence
    return records


def _module_records(data: dict[str, Any]) -> dict[str, Any]:
    """``ref`` -> the emitted module record, with its levels folded in so a per-level
    path (``trait_changes``, ``talent_changes``) resolves against the module the row
    refs -- the §V66.3 hoist moves those bundles between the two, and the evidence row
    is about the module either way.

    A list-valued key is CONCATENATED across levels, never taken from the first level:
    a stat the module grants only from level 2 (``stat_bonus.def`` on Ghost2's PUM-X) is
    just as real a path as one granted at level 1, and a first-level-wins merge would
    have failed the row rather than the code."""
    records: dict[str, Any] = {}
    for module in data.get("modules", []):
        merged = dict(module)
        for level in module.get("levels", []):
            for key, value in level.items():
                existing = merged.get(key)
                if isinstance(value, list) and isinstance(existing, list):
                    merged[key] = [*existing, *value]
                else:
                    merged.setdefault(key, value)
        records[module["game_id"]] = merged
    return records


def _assert_rows_typed(observations: list[dict[str, Any]], records: dict[str, Any]) -> int:
    """Assert every evidence row satisfies §V101; return how many rows were checked."""
    checked = 0
    for obs in observations:
        for row in obs["evidence"]:
            where = f"{obs['rule_id']} {row['ref']}.{row['field']}"
            # §V68: the ref names a real emitted record -- for a stage row that is the
            # stage's game_id, so a stage_code ref fails here rather than silently.
            assert row["ref"] in records, f"{where}: ref names no emitted record"
            # §V101: a real emitted field path, never a packed pseudo-field.
            assert "/" not in row["field"], f"{where}: packed pseudo-path"
            assert _resolve(records[row["ref"]], row["field"]), f"{where}: path resolves to nothing"
            # §V101: that field's scalar, never a packed string.
            value = row["value"]
            assert not isinstance(value, list | dict), f"{where}: value is not a scalar"
            if isinstance(value, str):
                assert "=" not in value and "," not in value, f"{where}: packed value {value!r}"
            # §V101: prose only -- no key=value, and no digits outside the §V85 level list.
            note = row.get("note")
            if note is not None:
                assert not _PACKED_NOTE.search(note), f"{where}: packed note {note!r}"
                if any(char.isdigit() for char in note):
                    assert _LEVEL_NOTE.match(note), f"{where}: number buried in note {note!r}"
            checked += 1
    return checked


# --- analyze_stage ------------------------------------------------------------------


def _analyze(conn: sqlite3.Connection, **kwargs: Any) -> dict[str, Any]:
    spec = build_analyze_stage_spec(lambda: conn)
    return spec.handler(depth="detailed", **kwargs).to_dict()["data"]  # type: ignore[no-any-return]


def _compare(conn: sqlite3.Connection, **kwargs: Any) -> dict[str, Any]:
    spec = build_compare_operator_modules_spec(lambda: conn)
    return spec.handler(mode="with_observations", **kwargs).to_dict()["data"]  # type: ignore[no-any-return]


def test_fixture_stage_evidence_rows_are_typed(fixture_conn: sqlite3.Connection) -> None:
    """Every fixture stage's evidence resolves against its own response (§V101/§V68)."""
    codes = [
        row[0] for row in fixture_conn.execute("SELECT stage_code FROM stages WHERE server = 'en'")
    ]
    assert codes, "fixture corpus carries no en stage"
    checked = 0
    for code in codes:
        data = _analyze(fixture_conn, server="en", stage_code=code)
        if "observations" not in data:
            continue
        checked += _assert_rows_typed(data["observations"], _stage_records(data))
    assert checked >= 5, f"only {checked} evidence rows exercised; the guard proves little"


def test_fixture_module_evidence_rows_are_typed(operator_conn: sqlite3.Connection) -> None:
    """The module analyzer's rows resolve against the comparison response (§V101)."""
    operators = [
        row[0] for row in operator_conn.execute("SELECT game_id FROM operators WHERE server = 'en'")
    ]
    checked = 0
    for game_id in operators:
        data = _compare(operator_conn, server="en", game_id=game_id, levels=[1, 2, 3])
        if not data.get("observations"):
            continue
        checked += _assert_rows_typed(data["observations"], _module_records(data))
    assert checked, "no module observation exercised"


def test_real_build_stage_evidence_rows_are_typed(build_conn: sqlite3.Connection) -> None:
    """The whole en stage corpus, not the handful the fixtures carry.

    Every one of the five rules that fire on the real build is exercised here, which is
    the only place the 3096 packed ``def/res`` rows and the 2453 stage_code refs were
    ever visible -- the fixtures reproduce the SHAPES, not the population.
    """
    stages = [
        row[0]
        for row in build_conn.execute(
            "SELECT game_id FROM stages WHERE server = 'en' ORDER BY stage_pk LIMIT 400"
        )
    ]
    assert len(stages) == 400
    checked = 0
    rules: set[str] = set()
    for game_id in stages:
        data = _analyze(build_conn, server="en", game_id=game_id)
        observations = data.get("observations")
        if not observations:
            continue
        rules.update(obs["rule_id"] for obs in observations)
        checked += _assert_rows_typed(observations, _stage_records(data))
    # §V96 non-degenerate: a guard that checked zero rows of the rules it names is not a
    # guard. These SIX are the whole registry as of §T210: ranged_arts joined them when the
    # §V30 bridge started filling the columns it decides from (B160), and the three that
    # could never fire were retired rather than left registered (§T210 c).
    assert rules == {
        "threat.aerial",
        "threat.def_res_skew",
        "threat.lane_route",
        "threat.pressure_spike",
        "threat.ranged_arts",
        "threat.tiles_deploy",
    }, rules
    assert checked > 500, f"only {checked} rows checked"


def test_real_build_module_evidence_rows_are_typed(build_conn: sqlite3.Connection) -> None:
    """The module analyzer over real operators -- where the fictional token path lived."""
    operators = [
        row[0]
        for row in build_conn.execute(
            "SELECT game_id FROM operators WHERE server = 'en' ORDER BY operator_pk LIMIT 120"
        )
    ]
    checked = 0
    token_rows = 0
    for game_id in operators:
        data = _compare(build_conn, server="en", game_id=game_id, levels=[1, 2, 3])
        if not data.get("observations"):
            continue
        records = _module_records(data)
        checked += _assert_rows_typed(data["observations"], records)
        token_rows += sum(
            1
            for obs in data["observations"]
            for row in obs["evidence"]
            if row["field"] == "talent_changes.applies_to"
        )
    assert checked > 100, f"only {checked} rows checked"
    # The token/summon label is live on the real corpus, so the path that names it is
    # exercised rather than merely declared -- it is the row B137's audit missed.
    assert token_rows, "no talent_changes.applies_to row exercised"


def test_stage_level_rows_ref_game_id_not_stage_code(build_conn: sqlite3.Connection) -> None:
    """§V68/B136 head-on, on a code the real build SHARES between two stages.

    ``4-4`` en names both ``main_04-04`` and ``main_04-04#f#`` (B139). A stage-level
    evidence row reffing the code cannot say which of the two it measured; reffing the
    game_id it can, and the ref joins to the stage block that carries the same key.
    """
    data = _analyze(build_conn, server="en", stage_code="4-4")
    game_id = data["stage"]["game_id"]
    assert game_id == "main_04-04" and data["stage"]["stage_code"] == "4-4"
    stage_rows = [
        row
        for obs in data["observations"]
        for row in obs["evidence"]
        if row["field"].startswith("metrics.")
    ]
    assert stage_rows, "4-4 fires no stage-level rule; pick a stage that does"
    assert {row["ref"] for row in stage_rows} == {game_id}
    # The paths those rows name are carried by this very response (§V101).
    for row in stage_rows:
        assert _resolve(data["stage"], row["field"])
