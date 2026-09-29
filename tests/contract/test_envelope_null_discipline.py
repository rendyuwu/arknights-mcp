"""The MECHANICAL null guard -- no tool ships a ``null`` leaf.

This is the fourth recurrence of one-surface-not-all. Null discipline was "rolled out"
across four passes (top-level keys, search locators, per-entity scalars, drop surfaces),
and each swept the surface then under review, so emit sites nobody
had looked at kept shipping nulls. A fifth hand-written per-surface pass would author the
fifth recurrence; what stops it is a guard that enumerates the REGISTRY rather than a
list of surfaces someone remembered to add.

Two properties make this guard mechanical rather than another list:

* it drives :meth:`ToolRegistry.names`, and **fails when a registered tool has no call
  set here**, so a new tool cannot join the server without joining the guard. A guard you
  have to remember to extend is the same construction that produced the recurrence.
* it walks the SERIALIZED envelope (what a transport actually puts on the wire), so it
  sees nested objects and list elements -- ``get_stage.map.map_version`` sat one level
  down inside ``map`` and no sweep had ever reached it.

Two arms. The offline arm builds the pinned fixture corpus and runs under the default
``pytest -q`` gate. The promoted-build arm (skipped without one) replays the entities that
made the live nulls visible in the first place: Amiya's locked talent, Cathy's unnamed
talents, ``guide_01``, ``st_07-04``, and a fully-flagged ``main_04-04``. Both are needed --
the fixture reproduces 7 of the 11 live null paths, and the build reproduces the 4 the
fixture's narrower corpus cannot (a stage with no display name, an operator whose talents
are unnamed, a talent variant with no blackboard, a map with no version).

The allowlist below is EMPTY, and that is the assertion. Null discipline permits "the
deliberate few"; today there are none, so any entry added here has to be justified in
the same commit that adds it.
"""

from __future__ import annotations

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
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
OPERATOR_ROOT = REPO_ROOT / "tests" / "fixtures" / "operator" / "en"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"
MANIFEST = REPO_ROOT / "data" / "current.json"

#: "allowlist the deliberate few". Empty by construction: no emitted key is
#: permitted to be null today. An addition here is an exception and needs its own
#: justification -- it is not a place to park a regression.
_ALLOWED_NULL_PATHS: frozenset[str] = frozenset()

#: The registry-driven call sets live in ``tests.support.tool_calls``: three
#: sweeps now share them, and a second copy would drift exactly where nobody is
#: looking -- which is the failure this guard exists to prevent.
BUILD = active_build()


def null_paths(node: object, path: str = "$") -> Iterator[str]:
    """Every path in a serialized envelope whose leaf is ``null``.

    Indices are generalized (``[0]`` -> ``[]``) so a failure names the FIELD rather than
    whichever row happened to carry it -- the fix belongs at the shaping site, not the row.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}"
            if value is None:
                yield child
            else:
                yield from null_paths(value, child)
    elif isinstance(node, list):
        for value in node:
            if value is None:
                yield f"{path}[]"
            else:
                yield from null_paths(value, f"{path}[]")


def _sweep(registry: ToolRegistry, calls: dict[str, tuple[dict[str, object], ...]]) -> list[str]:
    """Every ``(tool, path)`` pair that reached the wire as null, deduplicated."""
    found: set[str] = set()
    for name, body in serialized_envelopes(registry, calls):
        found |= {f"{name} {p}" for p in null_paths(body)}
    return sorted(found - _ALLOWED_NULL_PATHS)


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


def test_every_registered_tool_is_covered(fixture_conn: sqlite3.Connection) -> None:
    """A tool cannot be registered without entering this guard.

    This is the anti-recurrence clause. The failure mode being prevented is not "a null slipped
    through a covered tool" but "a whole tool was never swept", which is how four rollouts
    left five live nulls behind.
    """
    assert_every_tool_is_covered(registry_for(fixture_conn))


def test_no_tool_emits_a_null_leaf_on_the_fixture_corpus(fixture_conn: sqlite3.Connection) -> None:
    """Null discipline over the offline corpus -- runs on every ``pytest -q``."""
    assert _sweep(registry_for(fixture_conn), FIXTURE_CALLS) == []


def test_the_sweep_actually_reaches_nested_and_list_leaves(
    fixture_conn: sqlite3.Connection,
) -> None:
    """Guard the guard: a walker that stops at the top level would pass vacuously.

    Two of the five sites (``talents[].variants[].blackboard``,
    ``modules[].levels[].talent_changes``) are list elements two levels down, and
    ``map.map_version`` is inside a nested object. So the walk is asserted on a payload
    shaped like a real one, with a null at each of those depths.
    """
    payload = {
        "top": None,
        "nested": {"inner": None, "fine": 1},
        "rows": [{"deep": {"leaf": None}}, {"deep": {"leaf": 2}}],
        "bare": [None],
    }
    assert sorted(null_paths(payload)) == [
        "$.bare[]",
        "$.nested.inner",
        "$.rows[].deep.leaf",
        "$.top",
    ]


@pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)
def test_no_tool_emits_a_null_leaf_on_the_promoted_build() -> None:
    """Null discipline over the corpus the tools actually answer from.

    This arm is what caught the six null paths the earlier sweeps never named -- among them
    ``get_data_sources.active_snapshots[].commit_sha``, an unnamed talent's
    ``display_name``, and a stage with no ``display_name`` at all. None of those exist in
    the fixture tree, and no amount of fixture-side care would have surfaced them.
    """
    assert BUILD is not None
    with open_read_only(BUILD) as conn:
        assert _sweep(registry_for(conn), BUILD_CALLS) == []
