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


@dataclass(frozen=True)
class AllowlistResult:
    """Outcome of applying an allowlist: kept (sanitized) fields + dropped keys."""

    kept: dict[str, Any]
    dropped: list[str]


def apply_allowlist(
    raw: Mapping[str, Any],
    allowed: Collection[str],
    *,
    max_length: int = DEFAULT_MAX_TEXT_LENGTH,
) -> AllowlistResult:
    """Keep only ``allowed`` keys from ``raw``; sanitize kept string values.

    Non-string values are kept as-is (typed data). Keys outside the allowlist
    are dropped and reported (§21.2 unknown-field logging; §V18 exclusion).
    """
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in raw.items():
        if key not in allowed:
            dropped.append(key)
        elif isinstance(value, str):
            kept[key] = sanitize_text(value, max_length=max_length)
        else:
            kept[key] = value
    return AllowlistResult(kept=kept, dropped=sorted(dropped))
