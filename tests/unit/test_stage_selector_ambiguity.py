"""Stage-selector contract + shared-``stage_code`` disclosure.

A ``stage_code`` selects one stage but NAMES several: ``get_stage(stage_code="4-4")``
answered with ``main_04-04`` while ``search_stages("4-4")`` listed that AND
``main_04-04#f#``, with no note, no limitation, and no ``ambiguous`` status -- so the
four-star variant was reachable only through a game_id nothing pointed at, and the client
believed it had asked about "4-4" and been told about "4-4". The variant was already
truthful on OUTPUT; the INPUT axis was never closed.

These drive the three stage tools (``get_stage`` / ``analyze_stage`` /
``get_stage_drops``) end to end over the production read-only path and assert:

* a shared code discloses WHICH stage answered (with its truthful ``difficulty``)
  and names the alternates' game_ids -- the only handle that selects one of them;
* the pick is deterministic (lowest ``stage_pk``) and UNCHANGED by this task -- only the
  disclosure is added, so the change stays additive;
* a ``game_id`` lookup, and a code matching one stage, carry no such limitation (no noise
  on an unambiguous call);
* a long alternates list is bounded yet its COUNT stays exact;
* ``get_stage_drops``'s ``not_found`` -- which fires for a resolved stage that merely has
  no drop cache -- names the alternates in its suggested action, because a shared
  code can make the absence an artefact of the pick rather than of the data;
* the selector contract is stated PRE-call in every stage tool description,
  the one surface that reaches the client -- the published schema strips descriptions
  and can only carry the structural half.

The ambiguous fixture is the pinned 4-4 snapshot with the REAL ``main_04-04#f#`` variant
added: every field is transcribed from the shipped ``2026-07-28T170428Z`` build (same
``code`` "4-4", same ``levelId``, same ``apCost`` 18, same ``stageType`` MAIN, difficulty
FOUR_STAR), never invented -- a fixture that seeds the answer it asserts proves nothing.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from tests.support.drops import StageDropSeed, seed_item_across_stages, seed_stage_drop

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.db.repositories.stages import StageRepository
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.tools._shared import LEVEL_VARIANT_NOTE
from arknights_mcp.mcp.tools._stage_selector import (
    MAX_LISTED_ALTERNATES,
    STAGE_SELECTOR_NOTE,
    stage_ambiguity_drop_hint,
    stage_ambiguity_limitation,
)
from arknights_mcp.mcp.tools.drops import build_get_stage_drops_spec
from arknights_mcp.mcp.tools.stage import build_analyze_stage_spec, build_get_stage_spec
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.stages import AnalyzeStageInput, GetStageDropsInput, GetStageInput
from arknights_mcp.services.stages import MAX_STAGE_CODE_MATCHES, StageAmbiguity, _resolve_stage
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: The four-star variant, transcribed from the shipped build's own ``main_04-04#f#`` row
#: (stage_code "4-4", difficulty FOUR_STAR, stage_type MAIN, sanity 18, and the SAME level
#: file as the normal stage -- which is why both carry 117 tiles there).
VARIANT_GAME_ID = "main_04-04#f#"


def _snapshot_with_variant(tmp_path: Path) -> Path:
    """Copy the pinned 4-4 snapshot and add the real four-star variant entry."""
    root = tmp_path / "snapshot"
    shutil.copytree(FIXTURE_ROOT, root)
    table_path = root / "gamedata" / "excel" / "stage_table.json"
    table = json.loads(table_path.read_text(encoding="utf-8"))
    base = table["stages"]["main_04-04"]
    table["stages"][VARIANT_GAME_ID] = {
        **base,
        "stageId": VARIANT_GAME_ID,
        "difficulty": "FOUR_STAR",
    }
    table_path.write_text(json.dumps(table), encoding="utf-8")
    return root


def _candidate(tmp_path: Path, *, ambiguous: bool) -> Path:
    path = tmp_path / "cand.sqlite"
    root = _snapshot_with_variant(tmp_path) if ambiguous else FIXTURE_ROOT
    build_candidate(
        path,
        [ServerImport("en", LocalSnapshotAdapter(root, "en", "local_snapshot"), "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    return path


@pytest.fixture
def shared_code_conn(tmp_path: Path) -> sqlite3.Connection:
    """4-4 imported as TWO stages (``main_04-04`` + its four-star variant)."""
    return open_read_only(_candidate(tmp_path, ambiguous=True))


@pytest.fixture
def unique_code_conn(tmp_path: Path) -> sqlite3.Connection:
    """4-4 imported as ONE stage -- the unambiguous control."""
    return open_read_only(_candidate(tmp_path, ambiguous=False))


def _disclosures(env: object) -> list[str]:
    """The emitted limitations that talk about the shared stage_code."""
    payload = env.to_dict()  # type: ignore[attr-defined]
    return [lim for lim in (payload.get("limitations") or []) if "stage_code" in lim]


# --- the fixture itself: two real stages, one code ------------------------------


def test_fixture_imports_both_variants_under_one_code(
    shared_code_conn: sqlite3.Connection,
) -> None:
    # Guard the guard: if the variant stopped importing, every assertion below would
    # pass vacuously against a one-stage build (a fixture that cannot
    # exhibit the bug cannot catch it).
    matches = StageRepository(shared_code_conn).stages_by_code("en", "4-4", MAX_STAGE_CODE_MATCHES)
    assert [row.game_id for row in matches] == ["main_04-04", VARIANT_GAME_ID]
    assert [row.difficulty for row in matches] == ["NORMAL", "FOUR_STAR"]


def test_pick_is_the_lowest_stage_pk_and_unchanged(shared_code_conn: sqlite3.Connection) -> None:
    # The disclosure is ADDITIVE -- the stage a shared code resolves to is the
    # same one this lookup always returned (first by stage_pk), deterministically.
    stage, ambiguity = _resolve_stage(
        StageRepository(shared_code_conn), "en", stage_code="4-4", game_id=None
    )
    assert stage is not None and stage.game_id == "main_04-04"
    assert ambiguity == StageAmbiguity(
        stage_code="4-4",
        chosen_game_id="main_04-04",
        chosen_difficulty="NORMAL",
        alternates=(VARIANT_GAME_ID,),
        truncated=False,
    )


def test_game_id_selector_is_never_ambiguous(shared_code_conn: sqlite3.Connection) -> None:
    stage, ambiguity = _resolve_stage(
        StageRepository(shared_code_conn), "en", stage_code=None, game_id=VARIANT_GAME_ID
    )
    assert stage is not None and stage.game_id == VARIANT_GAME_ID
    assert ambiguity is None


# --- the disclosure on all three stage tools -------------------------


def test_get_stage_names_the_chosen_stage_and_the_alternate(
    shared_code_conn: sqlite3.Connection,
) -> None:
    env = build_get_stage_spec(lambda: shared_code_conn).handler(server="en", stage_code="4-4")
    assert env.status == "ok"
    (disclosure,) = _disclosures(env)
    # The chosen stage, its truthful difficulty, and the alternate's game_id -- the
    # only handle that reaches the four-star variant (the exact complaint).
    assert "2 stages" in disclosure
    assert "main_04-04, difficulty NORMAL" in disclosure
    assert VARIANT_GAME_ID in disclosure
    # The next step is an MCP-callable tool, never a CLI command.
    assert "search_stages" in disclosure


def test_analyze_stage_carries_the_same_disclosure(shared_code_conn: sqlite3.Connection) -> None:
    handler = build_analyze_stage_spec(lambda: shared_code_conn).handler
    for depth in ("summary", "standard", "detailed"):
        env = handler(server="en", stage_code="4-4", depth=depth)
        (disclosure,) = _disclosures(env)
        assert VARIANT_GAME_ID in disclosure, depth


def test_get_stage_drops_carries_the_disclosure_on_its_facts(tmp_path: Path) -> None:
    path = _candidate(tmp_path, ambiguous=True)
    # Drops on the stage the code resolves to, so the tool answers ok -- the numbers are
    # real, and the client still learns they describe main_04-04 alone.
    seed_stage_drop(path)
    conn = open_read_only(path)
    env = build_get_stage_drops_spec(lambda: conn).handler(server="en", stage_code="4-4")
    assert env.status == "ok"
    assert env.to_dict()["data"]["stage"]["game_id"] == "main_04-04"  # type: ignore[index]
    (disclosure,) = _disclosures(env)
    assert "2 stages" in disclosure
    assert VARIANT_GAME_ID in disclosure


def test_unambiguous_code_carries_no_disclosure(unique_code_conn: sqlite3.Connection) -> None:
    env = build_get_stage_spec(lambda: unique_code_conn).handler(server="en", stage_code="4-4")
    assert env.status == "ok"
    assert _disclosures(env) == []


def test_game_id_lookup_carries_no_disclosure(shared_code_conn: sqlite3.Connection) -> None:
    # The ambiguity is a property of the SELECTOR, not of the stage: asking by id is
    # unambiguous even where the code is shared, so no limitation rides.
    env = build_get_stage_spec(lambda: shared_code_conn).handler(
        server="en", game_id=VARIANT_GAME_ID
    )
    assert env.status == "ok"
    assert _disclosures(env) == []
    assert env.to_dict()["data"]["stage"]["difficulty"] == "FOUR_STAR"  # type: ignore[index]


# --- the drops dead end names a retryable handle --------------------


def test_drops_empty_answer_names_the_alternates(tmp_path: Path) -> None:
    # On the shipped build 206 shared codes have a first-by-order stage with NO drops
    # while a sibling under the SAME code HAS them, so "no drop data" was an artefact of
    # the silent pick. Seed exactly that: the picked stage has no cache, the sibling does.
    #
    # The status moved this answer from ``not_found`` to ``ok`` + an empty ``drops``, which
    # leaves no ``suggested_action`` field to carry the alternates -- so they MOVED to the
    # limitation surface (move a mandated fact, never delete it). The retry this
    # discloses is the entire point of the change and must survive the status change.
    path = _candidate(tmp_path, ambiguous=True)
    seed_item_across_stages(
        path, [StageDropSeed(stage_code="4-4", stage_game_id="zz_sibling_4-4", sanity_cost=18)]
    )
    conn = open_read_only(path)
    env = build_get_stage_drops_spec(lambda: conn).handler(server="en", stage_code="4-4")
    assert env.status == "ok"
    assert env.to_dict()["data"]["drops"] == []  # type: ignore[index]
    hint = next(lim for lim in env.limitations if "retry with one of those game_ids" in lim)
    assert VARIANT_GAME_ID in hint
    assert "zz_sibling_4-4" in hint
    assert "may be the stage that holds the drop data" in hint


def test_drops_empty_answer_adds_no_alternates_when_the_code_is_unique(
    unique_code_conn: sqlite3.Connection,
) -> None:
    # An unambiguous selector carries no noise: the empty answer still says WHY it is
    # empty, but nothing claims a sibling stage might hold the data.
    env = build_get_stage_drops_spec(lambda: unique_code_conn).handler(
        server="en", stage_code="4-4"
    )
    assert env.status == "ok"
    assert all("is also used by" not in lim for lim in env.limitations)
    assert any("lists no drops" in lim for lim in env.limitations)


# --- the alternates list is bounded, the count stays exact -----------


def _ambiguity(count: int, *, truncated: bool = False) -> StageAmbiguity:
    return StageAmbiguity(
        stage_code="LT-1",
        chosen_game_id="lt_01_01",
        chosen_difficulty="NORMAL",
        alternates=tuple(f"lt_{i:02d}_01" for i in range(2, 2 + count)),
        truncated=truncated,
    )


def test_long_alternate_list_is_summarised_but_counted_exactly() -> None:
    # The real ``LT-1`` group is 36 stages; naming all 35 alternates in one sentence is an
    # economy breach, and silently listing 8 would UNDER-state the ambiguity.
    (text,) = stage_ambiguity_limitation(_ambiguity(35))
    assert "36 stages" in text
    assert text.count("lt_") == MAX_LISTED_ALTERNATES + 1  # + the chosen stage
    assert f"and {35 - MAX_LISTED_ALTERNATES} more" in text


def test_capped_read_is_reported_as_open_ended() -> None:
    # A matching set that hit the service's bounded read must not present its
    # alternates (or its count) as complete.
    (text,) = stage_ambiguity_limitation(_ambiguity(MAX_STAGE_CODE_MATCHES - 1, truncated=True))
    assert f"at least {MAX_STAGE_CODE_MATCHES} stages" in text
    assert "and others" in text
    assert "more" not in text.split("The others are")[1].split(".")[0]


def test_no_ambiguity_emits_nothing() -> None:
    # Both disclosures are silent on an unambiguous selector -- a game_id lookup, or a
    # code matching exactly one stage, carries no alternates and so no noise.
    assert stage_ambiguity_limitation(None) == ()
    assert stage_ambiguity_drop_hint(None) == ()


def test_difficulty_is_omitted_when_the_stage_has_none() -> None:
    # An absent difficulty is not spelled as "difficulty None" on the wire.
    ambiguity = StageAmbiguity(
        stage_code="4-4",
        chosen_game_id="main_04-04",
        chosen_difficulty=None,
        alternates=(VARIANT_GAME_ID,),
        truncated=False,
    )
    (text,) = stage_ambiguity_limitation(ambiguity)
    assert "difficulty" not in text
    assert "main_04-04, the first of them" in text


# --- the contract is stated PRE-call ---------------------------


def test_every_stage_tool_description_states_the_selector_contract() -> None:
    from arknights_mcp.mcp.tools.drops import _TOOL_DESCRIPTION as DROPS_DESC
    from arknights_mcp.mcp.tools.stage import _ANALYZE_TOOL_DESCRIPTION, _TOOL_DESCRIPTION

    for description in (_TOOL_DESCRIPTION, _ANALYZE_TOOL_DESCRIPTION, DROPS_DESC):
        assert STAGE_SELECTOR_NOTE in description
    assert "exactly one of stage_code or game_id" in STAGE_SELECTOR_NOTE


def test_moved_level_variant_note_still_reaches_the_client(
    unique_code_conn: sqlite3.Connection,
) -> None:
    # The selector contract was paid for by MOVING the level_variant join-key
    # gloss out of get_stage's description, never by deleting it -- so it must arrive at
    # its new home: a limitation on the responses that emit the key it decodes. A move
    # that drops the text is a deletion with extra steps.
    handler = build_get_stage_spec(lambda: unique_code_conn).handler
    with_spawns = handler(server="en", stage_code="4-4", include_spawns=True).to_dict()
    assert LEVEL_VARIANT_NOTE in with_spawns["limitations"]
    assert with_spawns["data"]["spawns"]  # type: ignore[index]
    # ...and only there: a response with no spawn rows does not pay for a gloss about a
    # key it never emits.
    assert LEVEL_VARIANT_NOTE not in handler(server="en", stage_code="4-4").to_dict()["limitations"]


@pytest.mark.parametrize("model", [GetStageInput, AnalyzeStageInput, GetStageDropsInput])
def test_published_schema_offers_both_selectors_and_no_leaked_prose(model: type) -> None:
    # The schema's half of the contract is STRUCTURAL: both selectors are present and
    # optional (either may be given), which is what makes "exactly one" a rule the
    # description has to state -- a schema cannot express it here. Every
    # ``description`` keyword is stripped from the published schema, so the selector
    # prose lives in the tool description, never here (a Field description would be
    # silently dropped and the contract would ship nowhere).
    schema = tool_input_schema(model)
    properties = schema["properties"]
    assert {"server", "stage_code", "game_id"} <= set(properties)
    assert schema["required"] == ["server"]
    assert all("description" not in properties[f] for f in ("stage_code", "game_id"))
