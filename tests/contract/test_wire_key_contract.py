"""T198: the v0.3 wire-KEY rules, swept mechanically over every registered tool.

Three of the five T198 fixes are rules about what a key may be NAMED and what it may
MEAN. Each was found on one surface and each could have been closed on that one surface:

* **§V100/B134** -- ``get_stage_drops.ranking[]`` emitted ``{id, name}`` holding an ITEM
  id and an item display name, while the sibling ``get_item_drops.ranking[]`` emitted the
  same two keys holding a STAGE id and a stage CODE. One shape, flipped referents, and a
  ``name`` that was not a name, so an LLM mislabelled the column it rendered.
* **§V71 (d)/B140** -- ``talentIndex`` / ``requiredPotentialRank`` / ``unlockCondition``
  shipped camelCase inside a snake_case envelope, three lines from ``unlock_phase``.
* **§V99/B148** -- ``schema_version`` named the response contract at the envelope level
  and a DB migration id inside ``get_data_status.data``; ``status`` named the tool result
  and, in the same payload, a snapshot's import lifecycle.

B134's own root note is the reason these are swept rather than asserted per site: the
generic ``{id, name}`` spelling came from §V66 (1) itself, so *"fixing one call site would
leave the rule intact"*. A per-emitter assertion is that fix. What holds instead is a walk
over the SERIALIZED envelope of every registered tool, driven from
:meth:`ToolRegistry.names` -- the same construction §V85/B106 argued for and the same one
:mod:`tests.contract.test_envelope_null_discipline` runs for §V67, sharing its call sets
(``tests.support.tool_calls``, §V37) so a new tool joins all the sweeps at once or none.

Two arms, for the reason the null sweep has two: the fixture corpus runs under the default
gate, and the promoted build carries rows the fixture cannot (a real penguin cache, a real
multi-source snapshot list), which is where the ranking rows and the rarity scale live.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.drops import seed_stage_drop
from tests.support.tool_calls import (
    BUILD_CALLS,
    FIXTURE_CALLS,
    active_build,
    assert_every_tool_is_covered,
    registry_for,
    serialized_envelopes,
)

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
OPERATOR_ROOT = REPO_ROOT / "tests" / "fixtures" / "operator" / "en"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

BUILD = active_build()

#: §V71 (d): a wire key is snake_case. Matches an inner capital, which is what a
#: camelCase leak from the upstream dump looks like (``talentIndex``, ``unlockCondition``).
_CAMEL_CASE = re.compile(r"[a-z][A-Z]")

#: §V100: a ranked/list row may not key its entity by a bare ``id`` or ``name``; the key
#: must name the entity, because the sibling tool's row has the same shape and the other
#: referent. Read post-call from the key alone, or not at all.
_GENERIC_ROW_KEYS = frozenset({"id", "name"})

#: Keys that legitimately repeat a name across nesting levels because they mean the SAME
#: thing there. §V99 forbids one name for two MEANINGS, not one name used consistently.
#:
#: ``server`` / ``snapshot_id`` are the §V87 inline join keys, ``limitations`` is the
#: observation-level list beside the envelope's own, ``analyzer_version`` is the §V6 field
#: every observation must carry (mandated, and the same analyzer the envelope names), and
#: ``provenance`` inside ``get_operator.data.operator`` is the documented
#: ``include_provenance`` echo of the envelope's own attribution.
#:
#: Each entry is a claim that the two uses denote the same fact. That is a different thing
#: from B148's ``schema_version``, where the inner value was a DB migration id and the
#: outer a response-contract version -- two unrelated axes under one name. An addition
#: here has to survive that test, so it is not a place to park a collision.
_SAME_MEANING_AT_BOTH_LEVELS = frozenset(
    {"server", "snapshot_id", "limitations", "analyzer_version", "provenance"}
)


def _keys(node: object, path: str = "$") -> Iterator[tuple[str, str]]:
    """Every ``(key, generalized path)`` in a serialized envelope.

    Indices collapse to ``[]`` so a failure names the FIELD rather than whichever row
    happened to carry it -- the fix belongs at the shaping site, not at the row.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}"
            yield key, child
            yield from _keys(value, child)
    elif isinstance(node, list):
        for value in node:
            yield from _keys(value, f"{path}[]")


def _rows_under(node: object, key: str) -> Iterator[dict[str, object]]:
    """Every dict element of a list stored under ``key``, at any depth."""
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key and isinstance(value, list):
                yield from (row for row in value if isinstance(row, dict))
            yield from _rows_under(value, key)
    elif isinstance(node, list):
        for value in node:
            yield from _rows_under(value, key)


@pytest.fixture
def fixture_conn(tmp_path: Path) -> sqlite3.Connection:
    """The pinned 4-4 + Amiya fixture corpus, with a fresh drop cache."""
    path = tmp_path / "cand.sqlite"
    build_candidate(
        path,
        [
            ServerImport(
                "en", LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot"), "local_snapshot"
            ),
            ServerImport(
                "en", LocalSnapshotAdapter(OPERATOR_ROOT, "en", "local_snapshot"), "local_snapshot"
            ),
        ],
        registry=load_source_registry(REGISTRY),
    )
    seed_stage_drop(path)
    return open_read_only(path)


# --- §V71 (d)/B140: no camelCase reaches the wire ------------------------------


def test_no_tool_emits_a_camelcase_key_on_the_fixture_corpus(
    fixture_conn: sqlite3.Connection,
) -> None:
    # B140 was three named keys on two tools, but the leak class is structural: these are
    # decoded source blobs, so ANY key the upstream dump adds rides straight out unless
    # something looks. This looks.
    offenders = sorted(
        {
            f"{name} {path}"
            for name, body in serialized_envelopes(registry_for(fixture_conn), FIXTURE_CALLS)
            for key, path in _keys(body)
            if _CAMEL_CASE.search(key)
        }
    )
    assert offenders == [], f"camelCase keys on the wire (§V71 d): {offenders}"


def test_the_camelcase_detector_actually_fires() -> None:
    # Guard the guard: a regex that matched nothing would let this whole sweep pass
    # vacuously. B128 is the precedent -- a classifier that matched 0 real records shipped
    # green for two milestones. These are B140's own three keys, verbatim.
    assert _CAMEL_CASE.search("talentIndex")
    assert _CAMEL_CASE.search("requiredPotentialRank")
    assert _CAMEL_CASE.search("unlockCondition")
    assert not _CAMEL_CASE.search("talent_index")
    assert not _CAMEL_CASE.search("unlock_condition")
    # A leading capital or an all-caps token is not a camelCase leak.
    assert not _CAMEL_CASE.search("PHASE_2")


# --- §V100/B134: a ranked row names its entity ---------------------------------


def _ranking_offenders(registry_conn: sqlite3.Connection, calls: dict[str, object]) -> list[str]:
    found: list[str] = []
    for name, body in serialized_envelopes(registry_for(registry_conn), calls):  # type: ignore[arg-type]
        for row in _rows_under(body, "ranking"):
            for key in sorted(_GENERIC_ROW_KEYS & set(row)):
                found.append(f"{name} ranking[].{key}")
    return sorted(set(found))


def test_no_ranking_row_uses_a_generic_key_on_the_fixture_corpus(
    fixture_conn: sqlite3.Connection,
) -> None:
    assert _ranking_offenders(fixture_conn, FIXTURE_CALLS) == []


def test_ranking_rows_are_actually_produced_by_the_call_sets(
    fixture_conn: sqlite3.Connection,
) -> None:
    # Guard the guard: the sweep above passes vacuously if no call emits a ranking at all.
    # Both drop tools are called with include_efficiency, so both must produce rows.
    tools = {
        name
        for name, body in serialized_envelopes(registry_for(fixture_conn), FIXTURE_CALLS)
        if any(True for _ in _rows_under(body, "ranking"))
    }
    assert tools == {"get_stage_drops", "get_item_drops"}


def test_a_name_key_never_carries_a_code(fixture_conn: sqlite3.Connection) -> None:
    # §V100's second clause, and the subtler half of B134: ``get_item_drops`` put the
    # stage CODE ("1-7") under ``name`` while the stage's display name is "The Tyrant".
    # A ``*_name`` key must hold a display name; a code belongs in a ``*_code``.
    for name, body in serialized_envelopes(registry_for(fixture_conn), FIXTURE_CALLS):
        for row in _rows_under(body, "ranking"):
            for key, value in row.items():
                if not key.endswith("_name"):
                    continue
                entity = key.removesuffix("_name")
                code = row.get(f"{entity}_code")
                assert value != code, f"{name} ranking[].{key} carries the {entity} code"


# --- §V99/B148: one key name, one meaning, across envelope levels ---------------


def test_no_key_names_two_things_in_one_envelope(fixture_conn: sqlite3.Connection) -> None:
    # B148: ``schema_version`` was the response contract at the envelope level AND a DB
    # migration id inside ``data``; ``status`` was the tool result AND a snapshot's import
    # lifecycle. Both in one payload, undocumented, so a client could not tell which
    # governed. The rule is checked against the ENVELOPE-level names, which are the four a
    # client reads unconditionally and therefore the ones a nested reuse collides with.
    envelope_level = {"schema_version", "status", "analyzer_version", "provenance"}
    offenders = sorted(
        {
            f"{name} {path}"
            for name, body in serialized_envelopes(registry_for(fixture_conn), FIXTURE_CALLS)
            for key, path in _keys(body)
            if key in envelope_level
            and path.count(".") > 1
            and key not in _SAME_MEANING_AT_BOTH_LEVELS
        }
    )
    assert offenders == [], f"envelope key names reused for another meaning (§V99): {offenders}"


def test_the_collision_detector_would_have_caught_b148(fixture_conn: sqlite3.Connection) -> None:
    # Guard the guard: the sweep above passes if the exemption set swallows everything, so
    # the two names B148 actually collided on are asserted to be UNEXEMPT. If a later
    # commit parks ``schema_version`` or ``status`` in the exemption set to make a failure
    # go away, this fails instead -- which is the point.
    assert "schema_version" not in _SAME_MEANING_AT_BOTH_LEVELS
    assert "status" not in _SAME_MEANING_AT_BOTH_LEVELS
    # And the fixed shape is what actually ships: get_data_status carries the renamed keys
    # and neither echo. This is B148's own payload, checked end to end.
    body = dict(
        next(
            body
            for name, body in serialized_envelopes(registry_for(fixture_conn), FIXTURE_CALLS)
            if name == "get_data_status"
        )
    )
    data = body["data"]
    assert isinstance(data, dict)
    assert "db_schema_version" in data
    assert "schema_version" not in data
    assert "status" not in data and "analyzer_version" not in data
    for snapshot in data["snapshots"]:  # type: ignore[union-attr]
        assert "import_status" in snapshot
        assert "status" not in snapshot


# --- the promoted-build arm ----------------------------------------------------


@pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)
def test_wire_key_rules_hold_on_the_promoted_build() -> None:
    # The corpus the tools actually answer from: a real penguin cache (so the ranking rows
    # are the real ones B134 was found on), real multi-source snapshots, and real decoded
    # module blobs. The fixture cannot produce any of those.
    assert BUILD is not None
    with open_read_only(BUILD) as conn:
        assert_every_tool_is_covered(registry_for(conn))
        camel = sorted(
            {
                f"{name} {path}"
                for name, body in serialized_envelopes(registry_for(conn), BUILD_CALLS)
                for key, path in _keys(body)
                if _CAMEL_CASE.search(key)
            }
        )
        assert camel == [], f"camelCase keys on the wire (§V71 d): {camel}"
        assert _ranking_offenders(conn, BUILD_CALLS) == []
