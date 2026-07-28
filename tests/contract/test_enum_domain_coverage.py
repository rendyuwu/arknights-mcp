"""T194/T207: §V104's real-corpus guard -- every EMITTED enum value is documented (B142).

A description that names four of twelve ``rule_type`` values passes any test written
from the description itself: the four it names are there. The failure is only visible
from the DATA side, which is where B142 found it -- ``get_banners`` partitioned the pool
types into "standard (NORMAL/SINGLE/DOUBLE/LINKAGE) carries no featured op" versus the
rest, while the wire emitted seven further tokens the client could not place. Same class
as §V96: a domain must be COUNTED over the real corpus, never guessed from the schema.

So this guard reads the PROMOTED build (the exact rows the tools answer from), collects
the distinct value of every enum-valued column the tools emit, and asserts each one is
documented by the tool that emits it. It fails on the day upstream adds a thirteenth pool
type, which is the point.

T207 moved the HOME, not the requirement: §V104 (b) makes a static response-side
``enum_legend`` an EQUAL home for an OUTPUT domain, so the assertion is now against the
legend the emitting tool hoists rather than its description. That move is what let §V71
(f) become a number -- §V104 and §V71 (f) were both writing to the same bounded string
with no rule saying which one yields (B158). The check is STRICTER than the old one in
one respect: the legend must match the corpus in BOTH directions, so a value the wire
stopped emitting cannot linger as a documented token either.

Skipped when no build is promoted (the offline ``pytest -q`` gate builds fixtures, not a
full en+cn corpus); the unit-level pin in ``tests/unit/test_client_facing_text.py``
carries the same value sets so a text edit still regresses loudly in CI.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.mcp.tools._shared import (
    ENUM_LEGENDS,
    OPEN_ENUM_LIMITATIONS,
    TOOL_ENUM_LEGEND_FIELDS,
)
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"
MANIFEST = REPO_ROOT / "data" / "current.json"

#: ``(table, column, tool)`` for every enum-valued column a tool puts on the wire. The
#: tool named is the one whose description owns that field's domain (§V104).
_EMITTED_ENUM_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("stages", "difficulty", "get_stage"),
    ("stages", "difficulty", "search_stages"),
    ("stages", "difficulty", "search_entities"),
    ("stages", "stage_type", "get_stage"),
    ("enemies", "enemy_class", "get_enemy"),
    ("enemies", "enemy_class", "analyze_stage"),
    ("enemies", "motion_type", "get_enemy"),
    ("skills", "skill_type", "get_operator"),
    ("skills", "duration_type", "get_operator"),
    ("operators", "profession", "get_operator"),
    ("operators", "position", "get_operator"),
    ("items", "item_type", "get_stage_drops"),
    ("items", "item_type", "get_item_drops"),
    ("banners", "rule_type", "get_banners"),
)

#: ``difficulty`` is the one column whose wire domain is WIDER than the stored one:
#: ``TOUGH`` / ``EASY`` are derived at query time from a ``tough_``/``easy_`` game_id
#: whose stored difficulty says ``NORMAL`` (§V80/B84), so the stored set alone would let a
#: description drop them.
_DERIVED_EXTRA_VALUES: dict[str, tuple[str, ...]] = {"difficulty": ("TOUGH", "EASY")}


def _active_build() -> Path | None:
    """The promoted build's path, or ``None`` when nothing is promoted."""
    if not MANIFEST.is_file():
        return None
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    filename = manifest.get("database_filename")
    if not filename:
        return None
    path = REPO_ROOT / "data" / "builds" / str(filename)
    return path if path.is_file() else None


BUILD = _active_build()

pytestmark = pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)


@pytest.fixture(scope="module")
def conn() -> sqlite3.Connection:
    assert BUILD is not None
    return sqlite3.connect(f"file:{BUILD}?mode=ro", uri=True)


@pytest.fixture(scope="module")
def descriptions() -> dict[str, str]:
    def _no_conn() -> sqlite3.Connection:  # pragma: no cover - never called
        raise RuntimeError("descriptions need no connection")

    registry = build_tool_registry(
        _no_conn,
        registry=load_source_registry(REGISTRY),
        mode="local",
        image_refs_enabled=True,
    )
    return {spec.name: spec.description for spec in registry.specs()}


@pytest.mark.parametrize(("table", "column", "tool"), _EMITTED_ENUM_COLUMNS)
def test_every_emitted_enum_value_is_published_in_its_legend(
    conn: sqlite3.Connection, table: str, column: str, tool: str
) -> None:
    rows = conn.execute(
        f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL"  # noqa: S608
    ).fetchall()
    emitted = {str(value) for (value,) in rows}
    emitted |= set(_DERIVED_EXTRA_VALUES.get(column, ()))
    # Guard the guard: a typo'd table/column would make this vacuously pass.
    assert emitted, f"{table}.{column} yielded no values"
    # §V104 (b): the tool that emits the column must be the one that publishes its domain,
    # so the field has to be in that tool's legend set -- a legend hoisted by some OTHER
    # tool documents nothing for this one.
    assert column in TOOL_ENUM_LEGEND_FIELDS[tool], (tool, column)
    legend = ENUM_LEGENDS[column]
    undocumented = sorted(v for v in emitted if v not in legend)
    assert undocumented == [], (
        f"{tool} emits {column} values its enum_legend never names: {undocumented}"
    )
    # The other direction: a token the corpus stopped emitting is a stale claim about the
    # data. ``sp_type`` is exempt -- its numeric arm is deliberately NOT in the legend
    # (see below), so the legend is a strict subset there by design.
    if column != "sp_type":
        stale = sorted(v for v in legend if v not in emitted)
        assert stale == [], f"{tool} legend names {column} values the build never emits: {stale}"


def test_sp_type_numeric_fallback_is_disclosed_but_never_named(
    conn: sqlite3.Connection, descriptions: dict[str, str]
) -> None:
    # ``sp_type`` is the one enum whose stored domain is MIXED (§V99/B157): alongside the
    # three named tokens the source also stores a bare numeric code (``8`` on 1145 rows of
    # the promoted build), so the legend cannot present the names as a closed partition.
    # Asserted separately from the loop above because the honest disclosure is a phrase,
    # not a token -- and because the legend must NOT acquire an entry for the numeric arm:
    # what ``8`` means is unverified, and §V29/§V96 forbid guessing it. §V104 is explicit
    # that this disclosure is the FLOOR, not the resolution.
    rows = conn.execute("SELECT DISTINCT sp_type FROM skills WHERE sp_type IS NOT NULL")
    emitted = {str(value) for (value,) in rows}
    named = {"INCREASE_WITH_TIME", "INCREASE_WHEN_ATTACK", "INCREASE_WHEN_TAKEN_DAMAGE"}
    legend = ENUM_LEGENDS["sp_type"]
    for value in named & emitted:
        assert value in legend, value
    for value in emitted - named:
        assert value not in legend, f"legend invented a meaning for the raw code {value}"
    if emitted - named:
        assert "raw source code" in OPEN_ENUM_LIMITATIONS["sp_type"], sorted(emitted - named)
