"""A skill fact the source scopes PER LEVEL survives to the wire.

``skill_table`` gives every skill LEVEL its own ``name``, ``skillType``, ``durationType``
and ``spData.spType``. The importer read level 1's and stored it as the skill's, so for a
skill whose levels disagree three facts were dropped and a fourth was asserted for levels
it does not describe. It is not hypothetical: at the pinned upstream ``sktok_mjcsdw``
stores ``sp_type`` = ``8`` -- the bare code the domain rule declares undecidable -- while its own
level 2 sends the NAMED ``INCREASE_WITH_TIME``, and ``skill_type`` PASSIVE while level 2 is
AUTO. Nothing on the wire showed it: the flattened ``8`` reads exactly like the
open-domain case an earlier bug documented.

These drive the whole path (snapshot -> import -> repository -> service -> envelope)
because the flattening happened at import and the wire is where it had to be visible:

* a skill whose levels agree is UNCHANGED -- the value rides the skill, no level repeats
  it, which is what keeps this fix additive for 1597 of 1598 EN skills;
* a skill whose levels disagree omits the key on the skill (never a null, never level 1's
  value passed off as the skill's) and each level carries its own;
* the response says where those values went, since an absent key alone reads as "the
  source has none".

The fixture skills are uniform, so the varying case is built by editing a COPY of the real
fixture tree: the shape is transcribed from the real ``sktok_mjcsdw`` / ``sktok_sunmao``
rows rather than invented (a test that seeds its own invention proves nothing). The
corpus-side counts live in ``tests/contract/test_skill_enum_encoding.py``.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.tools.operator import (
    SKILL_LEVEL_VARIANCE_NOTE,
    build_get_operator_spec,
)
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "operator" / "en"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

_AMIYA = "char_002_amiya"
#: The fixture skill made to vary. Amiya's second skill has a single level in the fixture,
#: so the first (two-level) one is the one that can disagree with itself.
_VARYING_SKILL = "skchr_amiya_1"


def _snapshot(tmp_path: Path, *, vary: bool) -> Path:
    """A copy of the operator fixture tree; ``vary=True`` makes one skill differ by level."""
    root = tmp_path / "snapshot"
    shutil.copytree(FIXTURE_ROOT, root)
    if not vary:
        return root
    path = root / "gamedata" / "excel" / "skill_table.json"
    table = json.loads(path.read_text(encoding="utf-8"))
    levels = table[_VARYING_SKILL]["levels"]
    assert len(levels) >= 2, "the varying case needs a skill with at least two levels"
    # The real divergence, transcribed: level 1 carries the unnamed numeric spType and
    # PASSIVE, level 2 the named token and AUTO, plus the sktok_sunmao-style rename.
    levels[0]["spData"]["spType"] = 8
    levels[0]["skillType"] = "PASSIVE"
    levels[0]["name"] = "Connect"
    levels[1]["spData"]["spType"] = "INCREASE_WITH_TIME"
    levels[1]["skillType"] = "AUTO"
    levels[1]["name"] = "Engrave"
    path.write_text(json.dumps(table), encoding="utf-8")
    return root


def _conn(tmp_path: Path, *, vary: bool) -> sqlite3.Connection:
    path = tmp_path / f"cand-{vary}.sqlite"
    adapter = LocalSnapshotAdapter(_snapshot(tmp_path, vary=vary), "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    return open_read_only(path)


def _skills(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    env = build_get_operator_spec(lambda: conn).handler(
        server="en", game_id=_AMIYA, include_skills=True
    )
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    operator = data["operator"]
    assert isinstance(operator, dict)
    skills = operator["skills"]
    assert isinstance(skills, list)
    return {s["game_id"]: s for s in skills}  # type: ignore[index,union-attr]


@pytest.fixture
def uniform(tmp_path: Path) -> sqlite3.Connection:
    return _conn(tmp_path, vary=False)


@pytest.fixture
def varying(tmp_path: Path) -> sqlite3.Connection:
    return _conn(tmp_path, vary=True)


def test_uniform_skill_states_its_four_fields_once_on_the_skill(
    uniform: sqlite3.Connection,
) -> None:
    # Every level agrees, so the value rides the skill and no level
    # repeats it. This is the shape 1597 of 1598 EN skills have.
    skill = _skills(uniform)[_VARYING_SKILL]
    assert skill["display_name"] == "Arts Charge"
    assert skill["skill_type"] == "PASSIVE"
    assert skill["sp_type"] == "INCREASE_WITH_TIME"
    assert skill["duration_type"] == "NONE"
    levels = skill["levels"]
    assert isinstance(levels, list) and len(levels) == 2
    for level in levels:
        assert not {"display_name", "skill_type", "sp_type", "duration_type"} & set(level)


def test_uniform_response_carries_no_variance_note(uniform: sqlite3.Connection) -> None:
    # A caveat about a shape this response does not have is noise.
    env = build_get_operator_spec(lambda: uniform).handler(
        server="en", game_id=_AMIYA, include_skills=True
    )
    assert SKILL_LEVEL_VARIANCE_NOTE not in env.limitations


def test_varying_skill_omits_the_key_and_gives_every_level_its_own(
    varying: sqlite3.Connection,
) -> None:
    # The skill claims nothing it cannot claim for all its levels, and the
    # value the old flatten discarded is on the wire. The named sp_type its level 2 carries
    # is exactly what a level-1 read threw away in favour of the undecidable code.
    skill = _skills(varying)[_VARYING_SKILL]
    assert not {"display_name", "skill_type", "sp_type"} & set(skill)
    levels = skill["levels"]
    assert isinstance(levels, list)
    assert [(lv["display_name"], lv["skill_type"], lv["sp_type"]) for lv in levels] == [
        ("Connect", "PASSIVE", "8"),
        ("Engrave", "AUTO", "INCREASE_WITH_TIME"),
    ]
    # ``duration_type`` was NOT edited, so it stays uniform and stays hoisted: the four
    # fields are judged one by one, never as a block, and no level repeats this one.
    assert skill["duration_type"] == "NONE"
    assert all("duration_type" not in lv for lv in levels)


def test_varying_response_routes_the_client_to_the_levels(varying: sqlite3.Connection) -> None:
    # An omitted key alone would read as "the source has none". The note says
    # the values moved, and rides the response exactly once.
    env = build_get_operator_spec(lambda: varying).handler(
        server="en", game_id=_AMIYA, include_skills=True
    )
    assert env.limitations.count(SKILL_LEVEL_VARIANCE_NOTE) == 1
    assert "levels" in SKILL_LEVEL_VARIANCE_NOTE


def test_variance_note_stays_off_a_response_without_skills(varying: sqlite3.Connection) -> None:
    # The varying skill exists in this build, but a response that did not ask for skills
    # emits none, so a note about them would describe a payload the client never got.
    env = build_get_operator_spec(lambda: varying).handler(server="en", game_id=_AMIYA)
    assert SKILL_LEVEL_VARIANCE_NOTE not in env.limitations


def test_the_flattened_scalar_is_gone_from_storage_too(varying: sqlite3.Connection) -> None:
    # The stored row is where the defect lived: `skills.sp_type` asserted level 1's value for the
    # whole skill. It now holds only what every level shares, and the per-level values are
    # stored where the source scopes them.
    stored = varying.execute(
        "SELECT sp_type, skill_type, display_name FROM skills WHERE game_id = ?",
        (_VARYING_SKILL,),
    ).fetchone()
    assert stored == (None, None, None)
    levels = varying.execute(
        "SELECT sl.level, sl.sp_type FROM skill_levels sl JOIN skills s USING (skill_pk) "
        "WHERE s.game_id = ? ORDER BY sl.level",
        (_VARYING_SKILL,),
    ).fetchall()
    assert levels == [(1, "8"), (2, "INCREASE_WITH_TIME")]
