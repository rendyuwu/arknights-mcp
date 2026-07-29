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

T208 added the second half (B157): the three ``skills`` columns fed by the SHARED
``_enum_text`` coercion pin their whole emitted token set, split into the named arm and
the bare-code arm. ``sp_type`` is mixed by upstream design, ``skill_type`` and
``duration_type`` are named-only -- but only on this corpus, so the pin is what turns
"clean" from an assumption about the coercion into a measured fact about the data.

T209 widened that second half to both storage sides (B159/§V112): the three columns are
scoped PER LEVEL upstream, so a value now rides ``skills`` only while every level agrees
and ``skill_levels`` when they do not. Reading the skill row alone would let a token drop
out of the guard's view exactly when it starts varying.

T210 added the LIST-valued enum columns (B160): ``enemies.damage_types_json`` and
``enemy_levels.immunities_json`` store a JSON array, so the scalar ``SELECT DISTINCT``
above cannot read them and a column whose domain no one checks is how the substrate
stayed empty in the first place. ``enemy_levels.targeting`` is scalar and joins the
table above -- it was renamed OUT of a ``_json`` suffix precisely so it could (§V99).

Skipped when no build is promoted (the offline ``pytest -q`` gate builds fixtures, not a
full en+cn corpus); the unit-level pin in ``tests/unit/test_client_facing_text.py``
carries the same value sets so a text edit still regresses loudly in CI.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.mcp.tools._enum_legend import (
    ENUM_LEGENDS,
    OPEN_ENUM_LIMITATIONS,
    TOOL_ENUM_LEGEND_FIELDS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
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
    ("enemy_levels", "targeting", "get_enemy"),
    ("enemy_levels", "targeting", "analyze_stage"),
    ("skills", "skill_type", "get_operator"),
    ("skills", "duration_type", "get_operator"),
    ("operators", "profession", "get_operator"),
    ("operators", "position", "get_operator"),
    ("items", "item_type", "get_stage_drops"),
    ("items", "item_type", "get_item_drops"),
    ("banners", "rule_type", "get_banners"),
)

#: §T210/§V113 (B160): ``(table, json column, wire field, tool)`` for every enum-valued
#: column stored as a JSON ARRAY. They are separated only because the read differs -- the
#: contract is identical to the scalar table above, and so is the reason it exists: these
#: are the two columns the §V30 bridge was not filling at all.
_EMITTED_ENUM_LIST_COLUMNS: tuple[tuple[str, str, str, str], ...] = (
    ("enemies", "damage_types_json", "damage_types", "get_enemy"),
    ("enemies", "damage_types_json", "damage_types", "analyze_stage"),
    ("enemy_levels", "immunities_json", "immunities", "get_enemy"),
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


@pytest.mark.parametrize(("table", "column", "field", "tool"), _EMITTED_ENUM_LIST_COLUMNS)
def test_every_emitted_list_enum_value_is_published_in_its_legend(
    conn: sqlite3.Connection, table: str, column: str, field: str, tool: str
) -> None:
    """The list-valued twin of the scalar guard above (§V104 b; §T210).

    Both directions again: an undocumented token is a value a client cannot place, and a
    documented token the corpus never emits is a claim about data that is not there --
    which for these two columns is exactly the state B160 found them in (0 rows).
    """
    rows = conn.execute(
        f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL"  # noqa: S608
    ).fetchall()
    emitted: set[str] = set()
    for (raw,) in rows:
        decoded = json.loads(raw)
        assert isinstance(decoded, list), f"{table}.{column} is not a JSON list: {raw!r}"
        emitted |= {str(token) for token in decoded}
    # Guard the guard: a typo'd column, or a column back to 0 rows, must not pass silently.
    assert emitted, f"{table}.{column} yielded no values"
    assert field in TOOL_ENUM_LEGEND_FIELDS[tool], (tool, field)
    legend = ENUM_LEGENDS[field]
    undocumented = sorted(value for value in emitted if value not in legend)
    assert undocumented == [], f"{tool} emits {field} values its legend never names: {undocumented}"
    stale = sorted(value for value in legend if value not in emitted)
    assert stale == [], f"{tool} legend names {field} values the build never emits: {stale}"


#: §T208 (B157): the three ``skills`` columns the SHARED ``_enum_text`` coercion feeds, with
#: the numeric arm each one carries on the promoted build. ``sp_type`` is the mixed-encoding
#: case (§V99) -- upstream ships a name for three values and the bare int ``8`` for the
#: fourth, in the same file at the same pin -- while its two siblings are 100% named. The
#: point of pinning all three is that the siblings are clean by DATA, not by construction:
#: the same stringify runs on them, so the day upstream sends an int for ``skillType`` the
#: code would reach the wire exactly as silently, and only a corpus-side pin catches it.
_ENUM_TEXT_COLUMNS: tuple[tuple[str, frozenset[str]], ...] = (
    ("sp_type", frozenset({"8"})),
    ("skill_type", frozenset()),
    ("duration_type", frozenset()),
)


def _is_source_code(value: str) -> bool:
    """A stored enum value that is a bare number, i.e. a code the source never named."""
    return value.lstrip("-").isdigit()


#: §T209/§V112 (B159): the same three columns now live on BOTH sides -- on ``skills`` when
#: every level agrees, on ``skill_levels`` when they do not -- and both reach the wire. A
#: guard that read only the skill row would stop seeing a value the moment it became
#: per-level, which is exactly the case the openness disclosure has to cover.
_ENUM_TEXT_TABLES = ("skills", "skill_levels")


@pytest.mark.parametrize(("column", "numeric_arm"), _ENUM_TEXT_COLUMNS)
def test_shared_enum_coercion_columns_pin_their_emitted_token_set(
    conn: sqlite3.Connection, column: str, numeric_arm: frozenset[str]
) -> None:
    emitted: set[str] = set()
    for table in _ENUM_TEXT_TABLES:
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL"  # noqa: S608
        ).fetchall()
        emitted |= {str(value) for (value,) in rows}
    # Guard the guard: a typo'd column would make every assertion below vacuous.
    assert emitted, f"{column} yielded no values on {_ENUM_TEXT_TABLES}"
    numeric = {value for value in emitted if _is_source_code(value)}
    # A NEW bare code, or the first one on a sibling, must fail loudly and be verified
    # against the pinned upstream before any text claims it exists (§V29/§V96).
    assert numeric == set(numeric_arm), f"{column} numeric arm moved: {sorted(numeric)}"
    # The named arm is exactly the legend: no emitted name undocumented, no documented
    # name the build stopped emitting.
    assert emitted - numeric == set(ENUM_LEGENDS[column]), column
    for value in numeric:
        # §V104 (c)/§V29/§V96: the legend must NOT acquire an entry for a code whose
        # meaning is unverified -- disclosure makes the value legal, never decidable.
        assert value not in ENUM_LEGENDS[column], (
            f"legend invented a meaning for the raw {column} code {value}"
        )
    if numeric:
        # §V96 non-degenerate: the numeric arm is real on this corpus, so the openness
        # disclosure has to be shipped rather than merely available.
        text = OPEN_ENUM_LIMITATIONS[column]
        assert "never given a fabricated meaning" in text, column
        for value in numeric:
            assert value in text, (column, value)
    else:
        assert column not in OPEN_ENUM_LIMITATIONS, (
            f"{column} declares a mixed-encoding caveat it no longer needs"
        )


#: §T209/§V112 (B159): the four fields ``skill_table`` scopes per level, and how many
#: skills of the promoted build actually disagree with themselves on at least one of them
#: (en 2 -- ``sktok_mjcsdw`` sp_type/skill_type and ``sktok_sunmao`` display_name; cn 4 --
#: those two plus ``sktok_xbcbag`` / ``sktok_xbcbag2``, whose per-level names are the
#: creatures they capture). The count is pinned in both directions: a build where it drops
#: to zero would make every §V112 assertion here vacuous, and one where it grows is a new
#: skill to re-verify against the source before the wire quietly reshapes.
_LEVEL_SCOPED_COLUMNS = ("display_name", "skill_type", "sp_type", "duration_type")
_VARYING_SKILLS_PER_SERVER = {"en": 2, "cn": 4}


def _side_counts(conn: sqlite3.Connection, column: str) -> list[tuple[str, int, int]]:
    """``(server, skills with the value hoisted, skills with it per level)`` for ``column``."""
    return conn.execute(  # noqa: S608 -- column comes from the module-level tuple above
        f"""
        SELECT s.server,
               SUM(s.{column} IS NOT NULL),
               SUM(s.{column} IS NULL AND EXISTS (
                   SELECT 1 FROM skill_levels l
                   WHERE l.skill_pk = s.skill_pk AND l.{column} IS NOT NULL))
        FROM skills s GROUP BY s.server
        """
    ).fetchall()


@pytest.mark.parametrize("column", _LEVEL_SCOPED_COLUMNS)
def test_a_level_scoped_field_is_stored_on_exactly_one_side(
    conn: sqlite3.Connection, column: str
) -> None:
    """§V112 (a)/(b): the skill states what every level shares, the level what differs.

    Never both: a value on the skill row plus the same value repeated per level is the
    §V66.3 hoist done twice, and a client reading one side would not know the other exists.
    """
    both = conn.execute(
        f"""
        SELECT COUNT(*) FROM skills s WHERE s.{column} IS NOT NULL AND EXISTS (
            SELECT 1 FROM skill_levels l
            WHERE l.skill_pk = s.skill_pk AND l.{column} IS NOT NULL)
        """  # noqa: S608 -- column comes from the module-level tuple above
    ).fetchone()[0]
    assert both == 0, f"{column} is stored on both the skill and its levels for {both} skills"
    # Guard the guard: the corpus has to actually carry the column somewhere.
    assert any(hoisted or per_level for _, hoisted, per_level in _side_counts(conn, column))


def test_the_per_level_side_is_non_degenerate(conn: sqlite3.Connection) -> None:
    """§V96: skills whose levels really do disagree, counted -- not assumed to exist.

    Without this the §V112 storage split could be shipped over a corpus where nothing
    varies, and every assertion about it would pass while the case it exists for went
    unmeasured (B128's lesson, applied to the level axis).
    """
    varying = dict(
        conn.execute(
            """
            SELECT s.server, COUNT(DISTINCT s.skill_pk) FROM skills s JOIN skill_levels l
            ON l.skill_pk = s.skill_pk
            WHERE (s.display_name IS NULL AND l.display_name IS NOT NULL)
               OR (s.skill_type IS NULL AND l.skill_type IS NOT NULL)
               OR (s.sp_type IS NULL AND l.sp_type IS NOT NULL)
               OR (s.duration_type IS NULL AND l.duration_type IS NOT NULL)
            GROUP BY s.server
            """
        ).fetchall()
    )
    assert varying == _VARYING_SKILLS_PER_SERVER, varying


def test_the_named_sp_type_a_level_1_read_discarded_is_stored(conn: sqlite3.Connection) -> None:
    """The exact row B159 was filed on, in the corpus the tools answer from.

    ``sktok_mjcsdw`` used to store the unnamed code ``8`` for the whole skill; the named
    token its level 2 carries is now stored beside it instead of being overwritten.
    """
    rows = conn.execute(
        """
        SELECT s.server, l.level, l.sp_type FROM skills s JOIN skill_levels l
        ON l.skill_pk = s.skill_pk WHERE s.game_id = 'sktok_mjcsdw' ORDER BY s.server, l.level
        """
    ).fetchall()
    assert rows == [
        ("cn", 1, "8"),
        ("cn", 2, "INCREASE_WITH_TIME"),
        ("en", 1, "8"),
        ("en", 2, "INCREASE_WITH_TIME"),
    ], rows
    assert conn.execute(
        "SELECT sp_type FROM skills WHERE game_id = 'sktok_mjcsdw' AND server = 'en'"
    ).fetchone() == (None,)
