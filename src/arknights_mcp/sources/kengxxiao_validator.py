"""CN cross-validator (§T69, CI only): kengxxiao CN vs primary CN enemy stats.

Kengxxiao's CN ``enemy_database.json`` is an *independent* dump of the same
underlying CN game data as the primary ``arknights_assets_gamedata`` source, but
in a different top-level shape (a ``{"enemies": [{"Key", "Value"}]}`` KV list,
bridged by :func:`~arknights_mcp.importers.normalization.normalize_kengxxiao_enemy_database`).
Cross-checking that the two sources agree on shared enemy stats
(``maxHp``/``baseAttackTime``/``massLevel``/``motion``) gives confidence the
normalized field mappings (§V29) pull *real* values, not artifacts of one
source's serialization.

This module is validation-only (§C): it performs **no** network or database I/O
(the CI-only contract test does the fetching), kengxxiao is never a runtime
dependency, its values never override the primary source, and its data is never
committed — fetch → compare → discard. The comparison here is pure, so it is
exercised offline with synthetic fixtures and only fed real data in CI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arknights_mcp.importers.normalization import (
    normalize_enemy_database,
    normalize_kengxxiao_enemy_database,
)

#: The normalized level stats we cross-check (§T69). These are the renamed images
#: of the real ``maxHp``/``baseAttackTime``/``massLevel`` keys (§V29); comparing the
#: *normalized* values is exactly what proves the mapping pulls the same real value
#: from each source. ``motion`` is compared separately (it lives per-enemy, not
#: per-level, in both dumps).
COMPARED_LEVEL_STATS: tuple[str, ...] = ("hp", "attackInterval", "weight")

#: ``attackInterval`` is a float; allow a hair of tolerance for JSON float repr.
#: Integer stats (``hp``/``weight``) are compared exactly.
_FLOAT_EPS = 1e-6
_FLOAT_STATS: frozenset[str] = frozenset({"attackInterval"})


@dataclass(frozen=True)
class StatMismatch:
    """One shared cell where the two CN sources disagree.

    ``level_variant`` is ``None`` for a per-enemy ``motion`` mismatch.
    """

    game_id: str
    level_variant: int | None
    stat: str
    primary: Any
    kengxxiao: Any


@dataclass(frozen=True)
class CrossCheckReport:
    """Result of cross-checking two normalized CN enemy databases."""

    compared_enemies: int
    compared_cells: int
    mismatches: tuple[StatMismatch, ...]

    @property
    def agreement_rate(self) -> float:
        """Fraction of compared cells that agreed (``1.0`` when nothing compared is
        vacuously reported as ``0.0`` so an empty overlap never looks like success)."""
        if self.compared_cells == 0:
            return 0.0
        return 1.0 - len(self.mismatches) / self.compared_cells

    def describe(self, limit: int = 20) -> str:
        """Human-readable summary for a CI failure message."""
        head = (
            f"{self.compared_enemies} shared enemies, {self.compared_cells} cells compared, "
            f"{len(self.mismatches)} mismatches (agreement={self.agreement_rate:.4f})"
        )
        if not self.mismatches:
            return head
        rows = [
            f"  {m.game_id} L{m.level_variant} {m.stat}: "
            f"primary={m.primary!r} kengxxiao={m.kengxxiao!r}"
            for m in self.mismatches[:limit]
        ]
        if len(self.mismatches) > limit:
            rows.append(f"  … and {len(self.mismatches) - limit} more")
        return head + "\n" + "\n".join(rows)


def _values_agree(stat: str, primary: Any, kengxxiao: Any) -> bool:
    if stat in _FLOAT_STATS:
        try:
            return abs(float(primary) - float(kengxxiao)) <= _FLOAT_EPS
        except (TypeError, ValueError):
            return bool(primary == kengxxiao)
    return bool(primary == kengxxiao)


def _index_by_variant(database_norm: dict[str, Any]) -> dict[str, dict[int, dict[str, Any]]]:
    """``{"enemies": {...}}`` → ``{game_id: {level_variant: level_dict}}``."""
    enemies = database_norm.get("enemies")
    if not isinstance(enemies, dict):
        return {}
    out: dict[str, dict[int, dict[str, Any]]] = {}
    for game_id, entry in enemies.items():
        if not isinstance(game_id, str):
            continue
        levels = entry.get("levels") if isinstance(entry, dict) else None
        by_variant: dict[int, dict[str, Any]] = {}
        for level in levels if isinstance(levels, list) else []:
            if not isinstance(level, dict):
                continue
            variant = level.get("level")
            # bool is an int subclass; treat only real ints as a variant index.
            variant = variant if isinstance(variant, int) and not isinstance(variant, bool) else 0
            by_variant[variant] = level
        out[game_id] = by_variant
    return out


def cross_check_normalized(
    primary_norm: dict[str, Any],
    kengxxiao_norm: dict[str, Any],
    primary_motion: dict[str, str],
    kengxxiao_motion: dict[str, str],
) -> CrossCheckReport:
    """Compare two already-normalized CN enemy databases + motion maps (§V29).

    Only cells where *both* sources carry a non-``None`` value are compared: a stat
    present in one dump but absent in the other (schema/version drift) is skipped,
    never counted as a mismatch. A cell is one ``(enemy, level_variant, stat)``
    triple for level stats, plus one ``(enemy, motion)`` per shared enemy.
    """
    primary_idx = _index_by_variant(primary_norm)
    kengxxiao_idx = _index_by_variant(kengxxiao_norm)
    shared_ids = sorted(set(primary_idx) & set(kengxxiao_idx))

    mismatches: list[StatMismatch] = []
    compared_cells = 0
    for game_id in shared_ids:
        primary_variants = primary_idx[game_id]
        kengxxiao_variants = kengxxiao_idx[game_id]
        for variant in sorted(set(primary_variants) & set(kengxxiao_variants)):
            primary_level = primary_variants[variant]
            kengxxiao_level = kengxxiao_variants[variant]
            for stat in COMPARED_LEVEL_STATS:
                primary_value = primary_level.get(stat)
                kengxxiao_value = kengxxiao_level.get(stat)
                if primary_value is None or kengxxiao_value is None:
                    continue
                compared_cells += 1
                if not _values_agree(stat, primary_value, kengxxiao_value):
                    mismatches.append(
                        StatMismatch(game_id, variant, stat, primary_value, kengxxiao_value)
                    )
        primary_m = primary_motion.get(game_id)
        kengxxiao_m = kengxxiao_motion.get(game_id)
        if primary_m is not None and kengxxiao_m is not None:
            compared_cells += 1
            if primary_m != kengxxiao_m:
                mismatches.append(StatMismatch(game_id, None, "motion", primary_m, kengxxiao_m))

    return CrossCheckReport(
        compared_enemies=len(shared_ids),
        compared_cells=compared_cells,
        mismatches=tuple(mismatches),
    )


def cross_check_raw_enemy_databases(
    primary_raw: Any,
    kengxxiao_raw: Any,
) -> CrossCheckReport:
    """Normalize both raw CN enemy databases, then cross-check them (§T69, §V29, §V30).

    ``primary_raw`` is the ``arknights_assets_gamedata`` id-keyed dict; ``kengxxiao_raw``
    is the kengxxiao ``{"enemies": [{"Key", "Value"}]}`` KV list. Each is bridged
    through :mod:`arknights_mcp.importers.normalization` (§V30) so the comparison
    runs on the shared normalized shape.
    """
    primary_norm, primary_motion = normalize_enemy_database(primary_raw)
    kengxxiao_norm, kengxxiao_motion = normalize_kengxxiao_enemy_database(kengxxiao_raw)
    return cross_check_normalized(primary_norm, kengxxiao_norm, primary_motion, kengxxiao_motion)


__all__ = [
    "COMPARED_LEVEL_STATS",
    "StatMismatch",
    "CrossCheckReport",
    "cross_check_normalized",
    "cross_check_raw_enemy_databases",
]
