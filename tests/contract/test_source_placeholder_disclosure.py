"""T196: §V103's real-corpus guard -- a source mask never ships bare (B141).

B141 is fabrication-by-passthrough: Amiya's talent 0 is ``display_name: "？？？"`` with
variant 0 ``description: "？？？？？"`` -- a genuine locked-until-E2 mask in the source -- and
the server emitted both with zero disclosure, so a client LLM answers "her talent is
？？？" or, worse, fills the blank itself. §V26 makes an ABSENT field say so; a PRESENT
placeholder slipped underneath it.

The detection rule is a SHAPE (no word character survives stripping), and §V103 requires
it be keyed on the REAL token set rather than a guessed literal (§V96). So the first test
here does the counting: it runs the production predicate across every text column of the
promoted build and pins the partition it produces. That pin is the non-degeneracy
evidence -- it proves the rule selects something real (six sites, five distinct tokens),
that it selects nothing else (no CN or EN name is caught), and it fails on the day
upstream introduces a sixth token, which is when someone has to look at it.

The rest drive the actual tools, because a predicate that is right about the database and
a response that carries the disclosure are two different claims. B107 is the standing
lesson: a guard built on an invented fixture asserts its own invention. Every entity here
is transcribed from the shipped build.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.mcp.payload_hygiene import is_source_mask
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "config" / "data_sources.toml"
MANIFEST = REPO_ROOT / "data" / "current.json"

#: The §V103 scope as STORED column names (the wire keys are their snake_case selves:
#: ``gameplay_description`` reaches the client as ``description``). Same shape rule the
#: production scan uses on wire keys.
_NAME_LIKE_COLUMN = ("name", "description", "title", "code")

#: Tables that hold no client-facing entity text: pipeline bookkeeping, the FTS shadow
#: tables, and the analyzer/registry metadata. Excluded so the census measures the surface
#: a client actually reads.
_NON_ENTITY_TABLES = frozenset(
    {
        "analysis_findings",
        "analysis_rules",
        "data_sources",
        "entity_fts",
        "entity_fts_config",
        "entity_fts_content",
        "entity_fts_data",
        "entity_fts_docsize",
        "entity_fts_idx",
        "record_provenance",
        "schema_migrations",
        "source_policy_events",
        "source_snapshots",
    }
)

#: COUNTED over ``2026-07-28T170428Z-en-cn`` (en+cn), as ``(table, column, token) -> rows``.
#: Five distinct tokens over six sites. Pinned in BOTH directions: a new token means
#: upstream started masking something new and the disclosure has to be re-read against it,
#: while a token dropping out means the case a test below exercises no longer exists.
#:
#: ``-`` on ``skill_levels`` is the one that never reaches the wire today: all 28 rows
#: belong to ``sktok_*`` token skills, which ``parse_operators`` skips, so no operator
#: joins them (verified: the operator join returns zero rows). It is pinned anyway --
#: the day tokens become entities, the scan already covers them.
_EXPECTED_MASK_CENSUS: dict[tuple[str, str, str], int] = {
    ("skill_levels", "gameplay_description", "-"): 28,
    ("stages", "stage_code", "???"): 4,
    ("talent_levels", "gameplay_description", "-"): 4,
    ("stages", "display_name", "??:??:??"): 2,
    ("talents", "display_name", "？？？"): 2,
    ("talent_levels", "gameplay_description", "？？？？？"): 2,
}


def _active_build() -> Path | None:
    if not MANIFEST.is_file():
        return None
    filename = json.loads(MANIFEST.read_text(encoding="utf-8")).get("database_filename")
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
    return open_read_only(BUILD)


@pytest.fixture(scope="module")
def registry(conn: sqlite3.Connection) -> ToolRegistry:
    return build_tool_registry(
        lambda: conn,
        registry=load_source_registry(REGISTRY_PATH),
        mode="local",
        image_refs_enabled=True,
    )


def _disclosure(registry: ToolRegistry, name: str, **params: object) -> str | None:
    """The §V103 limitation on one tool call, or ``None`` when it carries none."""
    envelope = registry.get(name).handler(**params)
    assert envelope.status == "ok", (name, params, envelope.status)
    found = [text for text in envelope.limitations if text.startswith("placeholder text")]
    assert len(found) <= 1, "one disclosure per response, never one per masked field"
    return found[0] if found else None


def test_the_mask_token_set_is_counted_over_the_whole_build(conn: sqlite3.Connection) -> None:
    """§V96: the detection domain is measured on the corpus, never guessed.

    Scanning every text column (not just the ones a tool emits today) is deliberate: a
    column that starts reaching the wire tomorrow brings its masks with it, and this is
    where that shows up.
    """
    census: dict[tuple[str, str, str], int] = {}
    tables = [
        name
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if name not in _NON_ENTITY_TABLES
    ]
    for table in tables:
        columns = [
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})")
            if str(row[2]).upper() == "TEXT"
            and row[1].rsplit("_", maxsplit=1)[-1] in _NAME_LIKE_COLUMN
        ]
        for column in columns:
            rows = conn.execute(
                f"SELECT {column}, COUNT(*) FROM {table} "  # noqa: S608 -- names from the schema
                f"WHERE {column} IS NOT NULL GROUP BY {column}"
            )
            for value, count in rows:
                if is_source_mask(str(value)):
                    census[table, column, str(value)] = count
    # Guard the guard: a typo'd table/column filter would make this vacuously equal.
    assert census, "the census scanned no columns"
    assert census == _EXPECTED_MASK_CENSUS, census


def test_no_real_operator_or_stage_name_is_caught(conn: sqlite3.Connection) -> None:
    """The other half of non-degeneracy: the rule is precise, not merely non-empty.

    A predicate that flagged real names would bury the true masks under a disclosure on
    every response. ``\\w`` is Unicode-aware, so both regions' names are content -- this
    asserts it over the whole name corpus rather than on a couple of hand-picked strings.
    """
    for table in ("operators", "enemies", "items", "modules"):
        masked = [
            value
            for (value,) in conn.execute(
                f"SELECT display_name FROM {table} WHERE display_name IS NOT NULL"  # noqa: S608
            )
            if is_source_mask(str(value))
        ]
        assert masked == [], (table, masked[:5])
    # And the CN side really is present, so the assertion above is not passing on an
    # en-only corpus that never exercised the Unicode path.
    cn_names = conn.execute(
        "SELECT COUNT(*) FROM operators WHERE server = 'cn' AND display_name IS NOT NULL"
    ).fetchone()[0]
    assert cn_names > 100, cn_names


def test_b141_own_row_is_disclosed(registry: ToolRegistry) -> None:
    """The exact response B141 was filed on, through the tool a client calls."""
    text = _disclosure(
        registry, "get_operator", server="en", game_id="char_002_amiya", include_talents=True
    )
    assert text is not None
    # Both masked leaves are named, with their values, so the client can see WHICH field
    # is empty rather than being told something on this response is.
    assert "operator.talents[0].display_name is '？？？'" in text
    assert "operator.talents[0].variants[0].description is '？？？？？'" in text
    # The passthrough itself is still there: the server states the source value, never
    # substitutes one (§V26).
    envelope = registry.get("get_operator").handler(
        server="en", game_id="char_002_amiya", include_talents=True
    )
    talents = envelope.to_dict()["data"]["operator"]["talents"]  # type: ignore[index]
    assert talents[0]["display_name"] == "？？？"


def test_a_masked_effect_template_is_disclosed(registry: ToolRegistry) -> None:
    """The §V65 (a) grounding path is where a mask does the most damage.

    §V65 tells the client to trust the effect TEMPLATE over the raw blackboard key names,
    so a template of ``-`` is an instruction to trust nothing, delivered as content.
    """
    text = _disclosure(
        registry, "get_operator", server="en", game_id="char_4162_cathy", include_talents=True
    )
    assert text is not None
    assert "operator.talents[1].variants[0].description is '-'" in text


@pytest.mark.parametrize(
    ("game_id", "fragment"),
    [
        ("guide_01", "stage.stage_code is '???'"),
        ("st_07-04", "stage.display_name is '??:??:??'"),
    ],
)
def test_masked_stage_locators_are_disclosed(
    registry: ToolRegistry, game_id: str, fragment: str
) -> None:
    """A masked ``stage_code`` is the selector a client would try to call back with."""
    text = _disclosure(registry, "get_stage", server="en", game_id=game_id)
    assert text is not None and fragment in text


def test_an_unmasked_entity_carries_no_disclosure(registry: ToolRegistry) -> None:
    """Precision on the response surface: a clean stage gets no caveat.

    A disclosure attached to everything is a disclosure that means nothing.
    """
    assert _disclosure(registry, "get_stage", server="en", game_id="main_04-04") is None
    assert (
        _disclosure(
            registry, "get_operator", server="en", game_id="char_003_kalts", include_talents=True
        )
        is None
    )
