"""The single home for "every registered tool, and how to drive it" (§V37).

Several contract guards sweep the whole tool surface for a wire-shape property -- no null
leaf (§V67), no generic ranking key (§V100), no camelCase key (§V71 d). Each needs the
same two things: a registry over a corpus, and one call per tool that produces a
representative payload.

Those call sets live here rather than in whichever guard was written first, because of
what they are FOR. B135's null discipline was rolled out four times and each pass swept
the surfaces then under review, so a fifth hand pass would have authored the fifth
recurrence; the fix was a guard that enumerates :meth:`ToolRegistry.names` and FAILS when
a registered tool has no call set. That clause is only as good as the call sets it checks
against, so there must be exactly one set of them -- a second copy in a second guard would
drift, and the drifted copy would be the one covering the tool nobody looked at.

:func:`assert_every_tool_is_covered` is that clause, shared by every sweep that uses these
calls. Include flags are ON wherever a tool has them: an opt-in section that is off emits
nothing, and B135's own ``map.map_version`` lived behind ``include_map``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from arknights_mcp.mcp.tool_registry import ToolRegistry
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_TOML = REPO_ROOT / "config" / "data_sources.toml"
MANIFEST = REPO_ROOT / "data" / "current.json"

#: The offline arm: the pinned 4-4 + Amiya fixture corpus, driven under ``pytest -q``.
FIXTURE_CALLS: dict[str, tuple[dict[str, object], ...]] = {
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

#: The promoted-build arm. Same tools, pointed at rows carrying the shapes a hand-built
#: fixture does not have: ``guide_01`` (no display name), ``st_07-04``, ``char_4162_cathy``
#: (talents with no name), Amiya (a talent variant with no blackboard), and the real
#: penguin cache behind ``30012`` (Orirock Cube -- a T2 material, so the §V99 rarity scale
#: is exercised on a value that is not its own 0-indexed original).
BUILD_CALLS: dict[str, tuple[dict[str, object], ...]] = {
    **FIXTURE_CALLS,
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


def active_build() -> Path | None:
    """The promoted build's path, or ``None`` when nothing is promoted."""
    if not MANIFEST.is_file():
        return None
    filename = json.loads(MANIFEST.read_text(encoding="utf-8")).get("database_filename")
    if not filename:
        return None
    path = REPO_ROOT / "data" / "builds" / str(filename)
    return path if path.is_file() else None


def registry_for(conn: sqlite3.Connection) -> ToolRegistry:
    """The full tool registry over ``conn``, with every optional emit enabled."""
    return build_tool_registry(
        lambda: conn,
        registry=load_source_registry(REGISTRY_TOML),
        mode="local",
        image_refs_enabled=True,
    )


def assert_every_tool_is_covered(registry: ToolRegistry) -> None:
    """A tool cannot be registered without entering the sweeps (the anti-B135 clause).

    The failure mode this prevents is not "a bad value slipped through a covered tool" but
    "a whole tool was never swept", which is how four null-discipline rollouts left five
    live nulls behind. The reverse is asserted too: a call set for a tool that no longer
    exists is dead weight that would silently shrink every sweep using these calls.
    """
    registered = set(registry.names())
    assert registered - set(FIXTURE_CALLS) == set(), "a registered tool has no fixture call set"
    assert registered - set(BUILD_CALLS) == set(), "a registered tool has no build call set"
    assert set(FIXTURE_CALLS) - registered == set(), "a call set names no registered tool"


def serialized_envelopes(
    registry: ToolRegistry, calls: dict[str, tuple[dict[str, object], ...]]
) -> list[tuple[str, dict[str, object]]]:
    """Every ``(tool_name, serialized envelope)`` the call sets produce.

    Round-tripped through JSON on purpose: that is the transport's own serialization, so a
    sweep cannot pass on a shape only the in-process dataclass has.
    """
    out: list[tuple[str, dict[str, object]]] = []
    for name in registry.names():
        for params in calls[name]:
            envelope = registry.get(name).handler(**params)
            body: dict[str, object] = json.loads(json.dumps(envelope.to_dict()))
            out.append((name, body))
    return out
