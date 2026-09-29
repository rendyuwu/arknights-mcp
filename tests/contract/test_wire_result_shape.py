"""The ``tools/call`` result shape a CONTENT-ONLY client sees.

Every earlier sweep of the tool surface read ``structuredContent``. So did the stdio
smoke, the streamable-http smoke, and the transport-parity suite -- and
``test_result_carries_a_single_wire_copy`` went further and *asserted* ``content == []``
as the contract. A client that reads ``content`` and nothing else (LibreChat:
``result?.content ?? []``, with ``structuredContent`` absent from its bundle) therefore
got an empty answer to all 13 tools while ``initialize`` and ``tools/list`` looked
perfectly healthy, and three structured-reading clients hid it.

The guard is registry-driven for the same reason the null sweep is (
``tests.support.tool_calls``): a property asserted on whichever tool someone happened to
call is a property a new tool ships past. Four claims per registered tool:

1. ``content`` is non-empty and holds exactly one text block;
2. that block parses back to the *same* object as ``structuredContent`` -- a mirror, not
   a summary, and not a second (possibly stale) rendering;
3. the mirror is compact (the SDK's ``indent=2`` fallback is ~15% of dead wire bytes,
   which is what the deleted-copy transport was avoiding);
4. the envelope validates against the ``outputSchema`` the tool publishes, so the
   structured half is a declared contract rather than an undeclared extra.

Plus the accounting needed once two copies ride the wire: the emitted frame must be
no larger than what :func:`wire_size` measured (the gap, in the direction the one-copy
fix could not close).

Two arms, matching the null-discipline guard: the pinned fixture corpus under the default
``pytest -q`` gate, and the promoted build (skipped without one) whose payloads are the
ones that actually reach clients.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import jsonschema  # a hard dependency of the MCP SDK, which validates outputSchema with it
import pytest
from mcp import types
from tests.support.drops import seed_stage_drop
from tests.support.tool_calls import (
    BUILD_CALLS,
    FIXTURE_CALLS,
    active_build,
    assert_every_tool_is_covered,
    call_over_wire,
    registry_for,
    wire_results,
)

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.envelopes import (
    ResponseEnvelope,
    envelope_output_schema,
    mirror_text,
    wire_size,
)
from arknights_mcp.mcp.tool_registry import ToolRegistry, ToolSpec
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
OPERATOR_ROOT = REPO_ROOT / "tests" / "fixtures" / "operator" / "en"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

BUILD = active_build()


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


def _assert_mirrors_structured(name: str, result: types.CallToolResult) -> None:
    """Both halves present, identical, compact on one result."""
    assert result.isError is False, name
    # (1) a content-only client has something to read at all -- the whole defect.
    assert result.content, f"{name}: empty content renders as (No response) for a text client"
    assert len(result.content) == 1, name
    block = result.content[0]
    assert isinstance(block, types.TextContent), name
    # (2) the mirror IS the structured payload, not a paraphrase of it.
    assert result.structuredContent is not None, name
    assert json.loads(block.text) == result.structuredContent, name
    # (3) compact: no indented copy (the ~15% the one-copy transport was avoiding).
    assert "\n" not in block.text, name
    assert '", "' not in block.text, name


def _assert_matches_output_schema(name: str, registry: ToolRegistry, body: object) -> None:
    """The payload validates against the schema the tool publishes."""
    schema = registry.get(name).to_mcp_tool().outputSchema
    assert schema is not None, f"{name}: no outputSchema published"
    jsonschema.validate(instance=body, schema=schema)


# --- the registry-driven sweep (the anti-regression clause) ----------------------


def test_every_registered_tool_is_covered(fixture_conn: sqlite3.Connection) -> None:
    # A tool cannot be registered without entering this guard.
    assert_every_tool_is_covered(registry_for(fixture_conn))


def test_every_tool_declares_the_shared_envelope_output_schema(
    fixture_conn: sqlite3.Connection,
) -> None:
    # Before this guard the string "outputSchema" appeared nowhere in src/, so
    # tools/list never told a client that results carry structured output -- which is
    # exactly why reading ``content`` alone is a reasonable client design.
    registry = registry_for(fixture_conn)
    expected = envelope_output_schema()
    for tool in registry.to_mcp_tools():
        assert tool.outputSchema == expected, tool.name
        # One home: the schema is the envelope's, not a per-tool hand-copy that can drift.
        assert tool.outputSchema is not expected


def test_every_tool_mirrors_its_envelope_into_content_on_the_fixture_corpus(
    fixture_conn: sqlite3.Connection,
) -> None:
    # The full result contract over the offline corpus -- runs on every ``pytest -q``.
    registry = registry_for(fixture_conn)
    results = wire_results(registry, FIXTURE_CALLS)
    assert len(results) >= len(registry.names())
    for name, result in results:
        _assert_mirrors_structured(name, result)
        _assert_matches_output_schema(name, registry, result.structuredContent)


@pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)
def test_every_tool_mirrors_its_envelope_into_content_on_the_promoted_build() -> None:
    # The corpus clients actually answer from: real payloads (a fully-flagged
    # main_04-04, the penguin drop cache, the live announcement/banner windows), where
    # the mirror is tens of KB rather than a fixture's few hundred bytes.
    assert BUILD is not None
    with open_read_only(BUILD) as conn:
        registry = registry_for(conn)
        for name, result in wire_results(registry, BUILD_CALLS):
            _assert_mirrors_structured(name, result)
            _assert_matches_output_schema(name, registry, result.structuredContent)


# --- accounting once two copies ride the wire ------------------------------


def test_measured_cap_upper_bounds_the_emitted_frame(fixture_conn: sqlite3.Connection) -> None:
    # The cap is enforced on ``wire_size``; if the transport emitted more
    # bytes than that measured, the cap would be back to bounding the wrong number.
    registry = registry_for(fixture_conn)
    for name in registry.names():
        params = FIXTURE_CALLS[name][0]
        envelope = registry.get(name).handler(**params)
        result = call_over_wire(registry, name, params)
        emitted = len(result.model_dump_json(exclude_none=True).encode("utf-8"))
        assert emitted <= wire_size(envelope), f"{name}: emitted {emitted} > measured"


def test_wire_size_counts_the_mirror_it_ships(fixture_conn: sqlite3.Connection) -> None:
    # Guard the guard: a ``wire_size`` that forgot the mirror would still upper-bound
    # nothing but itself. The measure must exceed one copy by roughly the mirror's bytes.
    registry = registry_for(fixture_conn)
    envelope = registry.get("get_stage").handler(**FIXTURE_CALLS["get_stage"][0])
    one_copy = len(json.dumps(envelope.to_dict()).encode("utf-8"))
    assert wire_size(envelope) >= one_copy + len(mirror_text(envelope).encode("utf-8"))


# --- the declared schema is enforced, not decorative ---------------------------


def test_declared_output_schema_is_enforced_on_the_live_path(
    fixture_conn: sqlite3.Connection,
) -> None:
    # The transport returns (content, structured) rather than a prebuilt
    # CallToolResult precisely so the SDK validates structuredContent against the
    # declared schema on every call -- a prebuilt result returns before that check
    # (mcp/server/lowlevel/server.py). Register a tool whose envelope carries a status
    # outside the status vocabulary and assert the wire rejects it.
    registry = registry_for(fixture_conn)
    bogus = ResponseEnvelope(status="not_a_status", data={"x": 1})  # type: ignore[arg-type]
    registry.register(
        ToolSpec(
            name="get_bogus_status",
            title="Bogus",
            description="Test-only spec whose envelope violates the declared output schema.",
            handler=lambda: bogus,
        )
    )
    result = call_over_wire(registry, "get_bogus_status", {})
    assert result.isError is True
    assert result.structuredContent is None
