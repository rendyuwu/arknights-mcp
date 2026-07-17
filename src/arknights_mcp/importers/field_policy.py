"""Explicit field allowlist for imported gameplay data (SPEC §V18; PRD 10.2).

The importer parses *only* allowlisted source fields; unused prose and unknown
fields are dropped. Kept string values are sanitized (control chars stripped,
length capped). Each allowlist is versioned via ``FIELD_POLICY_VERSION`` so a
policy change is recorded on every snapshot and provenance row.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from arknights_mcp.util.text import DEFAULT_MAX_TEXT_LENGTH, sanitize_text

#: Bump when any allowlist below changes; stored on snapshots + provenance.
FIELD_POLICY_VERSION = "1"

# --- Allowlisted SOURCE fields per record type (V18) -------------------------
# Prose fields (e.g. "description") are intentionally absent and thus excluded.

ENEMY_HANDBOOK_ALLOWLIST: frozenset[str] = frozenset(
    {"enemyId", "name", "enemyLevel", "attackType", "motionType"}
)

ENEMY_LEVEL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "level",
        "hp",
        "atk",
        "def",
        "res",
        "attackInterval",
        "attackRange",
        "moveSpeed",
        "weight",
        "lifePointReduction",
        "blockBehavior",
        "targeting",
        "immunities",
        "abilities",
    }
)

ZONE_ALLOWLIST: frozenset[str] = frozenset({"zoneId", "zoneName", "type"})

STAGE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "stageId",
        "code",
        "name",
        "zoneId",
        "stageType",
        "difficulty",
        "apCost",
        "recommendedLevel",
        "maxLifePoints",
        "levelId",
    }
)

#: Structural spawn-action fields kept in ``stage_spawns.source_fragment_json``.
#: The raw wave action is untrusted and may carry prose/injection fields; only
#: these known structural keys are retained (§V18 "known keys only, no prose").
SPAWN_ACTION_ALLOWLIST: frozenset[str] = frozenset(
    {
        "enemyId",
        "levelVariant",
        "routeIndex",
        "spawnTime",
        "count",
        "interval",
        "spawnGroup",
        "hidden",
    }
)


@dataclass(frozen=True)
class AllowlistResult:
    """Outcome of applying an allowlist: kept (sanitized) fields + dropped keys."""

    kept: dict[str, Any]
    dropped: list[str]


def sanitize_value(value: Any, *, max_length: int = DEFAULT_MAX_TEXT_LENGTH) -> Any:
    """Recursively sanitize every string leaf (and key) of a kept value (§V18).

    A kept value may be a structured dict/list (e.g. ``abilities``, ``immunities``,
    ``specialProperties``, route ``checkpoints``) whose nested strings are just as
    untrusted as top-level ones: control/format/bidi chars must be stripped and
    every string length-capped before the value is JSON-encoded into a ``*_json``
    column and later surfaced to a client. Non-string scalars pass through.
    """
    if isinstance(value, str):
        return sanitize_text(value, max_length=max_length)
    if isinstance(value, Mapping):
        return {
            sanitize_text(str(k), max_length=max_length): sanitize_value(v, max_length=max_length)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [sanitize_value(item, max_length=max_length) for item in value]
    return value


def apply_allowlist(
    raw: Mapping[str, Any],
    allowed: Collection[str],
    *,
    max_length: int = DEFAULT_MAX_TEXT_LENGTH,
) -> AllowlistResult:
    """Keep only ``allowed`` keys from ``raw``; sanitize kept values (§V18).

    Every kept value is sanitized recursively (:func:`sanitize_value`): string
    leaves nested inside kept dict/list values are stripped of control chars and
    length-capped, not just top-level strings. Keys outside the allowlist are
    dropped and reported (§21.2 unknown-field logging; §V18 exclusion).
    """
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in raw.items():
        if key not in allowed:
            dropped.append(key)
        else:
            kept[key] = sanitize_value(value, max_length=max_length)
    return AllowlistResult(kept=kept, dropped=sorted(dropped))
