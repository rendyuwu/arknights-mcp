"""T196: §V67's MECHANICAL null guard -- no tool ships a ``null`` leaf (B135).

B135 is the fourth recurrence of one-surface-not-all. Null discipline was "rolled out"
in T130 (top-level keys), T170 (search locators), T180 (per-entity scalars) and T184
(drop surfaces), and each pass swept the surface then under review, so emit sites nobody
had looked at kept shipping nulls. A fifth hand-written per-surface pass would author the
fifth recurrence; what stops it is a guard that enumerates the REGISTRY rather than a
list of surfaces someone remembered to add.

Two properties make this guard mechanical rather than another list:

* it drives :meth:`ToolRegistry.names`, and **fails when a registered tool has no call
  set here**, so a new tool cannot join the server without joining the guard. A guard you
  have to remember to extend is the same construction that produced B135.
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

The allowlist below is EMPTY, and that is the assertion. §V67 permits "the deliberate
few"; today there are none, so any entry added here has to be justified against §V67 in
the same commit that adds it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.drops import seed_stage_drop

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
OPERATOR_ROOT = REPO_ROOT / "tests" / "fixtures" / "operator" / "en"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"
MANIFEST = REPO_ROOT / "data" / "current.json"

#: §V67 "allowlist the deliberate few". Empty by construction: no emitted key is
#: permitted to be null today. An addition here is a §V67 exception and needs its own
#: justification -- it is not a place to park a regression.
_ALLOWED_NULL_PATHS: frozenset[str] = frozenset()

#: Every registered tool, with the calls this guard drives it through. Keyed by tool name
#: so :func:`test_every_registered_tool_is_covered` can fail on a tool that is registered
#: but unlisted. Include flags are ON wherever a tool has them: an opt-in section that is
#: off emits nothing, and B135's own ``map.map_version`` lives behind ``include_map``.
_FIXTURE_CALLS: dict[str, tuple[dict[str, object], ...]] = {
    "search_entities": ({"query": "drone"}, {"query": "4-4", "entity_type": "stage"}),
    "search_stages": ({"query": "4-4"},),
    "get_stage": (
        {
            "server": "en",
            "stage_code": "4-4",
            "include_map": True,
            "include_routes": True,
            "include_spawns": True,
            "include_map_image": True,
        },
    ),
    "get_enemy": ({"server": "en", "game_id": "enemy_1007_slime"},),
    "get_operator": (
        {
            "server": "en",
            "game_id": "char_002_amiya",
            "include_skills": True,
            "include_talents": True,
            "include_modules": True,
            "include_phases": True,
            "include_provenance": True,
        },
    ),
    "compare_operator_modules": ({"server": "en", "game_id": "char_002_amiya"},),
    "analyze_stage": ({"server": "en", "stage_code": "4-4", "depth": "detailed"},),
    "get_stage_drops": ({"server": "en", "stage_code": "4-4", "include_efficiency": True},),
    "get_item_drops": ({"server": "en", "game_id": "sugar", "include_efficiency": True},),
    "get_announcements": ({"server": "en"},),
    "get_banners": ({"server": "en"},),
    "get_data_status": ({},),
    "get_data_sources": ({},),
}

#: The promoted-build arm. Same tools, but pointed at the rows that carry the absences a
#: hand-built fixture does not have: ``guide_01`` (no display name), ``st_07-04``,
#: ``char_4162_cathy`` (talents with no name), Amiya (a talent variant with no blackboard).
_BUILD_CALLS: dict[str, tuple[dict[str, object], ...]] = {
    **_FIXTURE_CALLS,
    "get_stage": (
        {
            "server": "en",
            "game_id": "main_04-04",
            "include_map": True,
            "include_routes": True,
            "include_spawns": True,
            "include_map_image": True,
        },
        {"server": "en", "game_id": "guide_01"},
        {"server": "en", "game_id": "st_07-04"},
    ),
    "get_operator": (
        {
            "server": "en",
            "game_id": "char_002_amiya",
            "include_skills": True,
            "include_talents": True,
            "include_modules": True,
            "include_phases": True,
            "include_provenance": True,
        },
        {
            "server": "en",
            "game_id": "char_4162_cathy",
            "include_skills": True,
            "include_talents": True,
            "include_modules": True,
        },
    ),
    "search_stages": ({"query": "4-4"}, {"query": "Lone Trail"}),
    "analyze_stage": ({"server": "en", "game_id": "main_04-04", "depth": "detailed"},),
    "get_stage_drops": ({"server": "en", "game_id": "main_04-04", "include_efficiency": True},),
    "get_item_drops": ({"server": "en", "game_id": "30012", "include_efficiency": True},),
}


def _active_build() -> Path | None:
    """The promoted build's path, or ``None`` when nothing is promoted."""
    if not MANIFEST.is_file():
        return None
    filename = json.loads(MANIFEST.read_text(encoding="utf-8")).get("database_filename")
    if not filename:
        return None
    path = REPO_ROOT / "data" / "builds" / str(filename)
    return path if path.is_file() else None


BUILD = _active_build()


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


def _registry_for(conn: sqlite3.Connection) -> ToolRegistry:
    return build_tool_registry(
        lambda: conn,
        registry=load_source_registry(REGISTRY),
        mode="local",
        image_refs_enabled=True,
    )


def _sweep(registry: ToolRegistry, calls: dict[str, tuple[dict[str, object], ...]]) -> list[str]:
    """Every ``(tool, path)`` pair that reached the wire as null, deduplicated."""
    found: set[str] = set()
    for name in registry.names():
        for params in calls[name]:
            envelope = registry.get(name).handler(**params)
            # Round-trip through JSON: this is the transport's own serialization, so the
            # guard cannot pass on a shape only the in-process dataclass has.
            body = json.loads(json.dumps(envelope.to_dict()))
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

    This is the anti-B135 clause. The failure mode being prevented is not "a null slipped
    through a covered tool" but "a whole tool was never swept", which is how four rollouts
    left five live nulls behind.
    """
    registered = set(_registry_for(fixture_conn).names())
    assert registered - set(_FIXTURE_CALLS) == set(), "a registered tool has no null-sweep call"
    assert registered - set(_BUILD_CALLS) == set()
    # And the reverse: a call set for a tool that no longer exists is dead weight that
    # would silently shrink the sweep.
    assert set(_FIXTURE_CALLS) - registered == set()


def test_no_tool_emits_a_null_leaf_on_the_fixture_corpus(fixture_conn: sqlite3.Connection) -> None:
    """§V67 over the offline corpus -- runs on every ``pytest -q``."""
    assert _sweep(_registry_for(fixture_conn), _FIXTURE_CALLS) == []


def test_the_sweep_actually_reaches_nested_and_list_leaves(
    fixture_conn: sqlite3.Connection,
) -> None:
    """Guard the guard: a walker that stops at the top level would pass vacuously.

    Two of B135's five sites (``talents[].variants[].blackboard``,
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
    """§V67 over the corpus the tools actually answer from.

    This arm is what caught the six null paths B135 never named -- among them
    ``get_data_sources.active_snapshots[].commit_sha``, an unnamed talent's
    ``display_name``, and a stage with no ``display_name`` at all. None of those exist in
    the fixture tree, and no amount of fixture-side care would have surfaced them.
    """
    assert BUILD is not None
    with open_read_only(BUILD) as conn:
        assert _sweep(_registry_for(conn), _BUILD_CALLS) == []
