"""Internal module-comparison service (§T45): the single domain entry point both
transports call to compare one operator's modules across potential levels (§V14).

Given a read-only SQLite connection and a ``(server, game_id)`` selector, it
resolves the operator (region-scoped, §V5), loads each of the operator's modules
and their levels, and projects them to the requested potential levels (a subset of
{1, 2, 3}) so a client sees the per-level change bundles side by side. A requested
level a module does not define is marked ``present=False`` rather than omitted, so
"absent level" is distinguishable from "no change".

``mode`` selects the response shape: ``facts_only`` returns the typed comparison
only; ``with_observations`` additionally runs the deterministic module analyzer
(§T46) and returns its evidence-backed observations (§V6) -- capability facts, never
a "mandatory"/"best" verdict (§V7). The stored structural JSON was allowlisted +
sanitized at import (§V18/§V31) and is decoded here (never prose, §V16).

Read-only + parameterized SQL only (§V2): the parameterized ``SELECT``s live in
:class:`~arknights_mcp.db.repositories.operators.OperatorRepository` (§T20), reused
here (§V37); this service only reads through it and never mutates the database. It
does not open the connection (callers pass one in), so both transports share this
exact function (§V14).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from arknights_mcp.analyzers import (
    ModuleAnalysisContext,
    ModuleInput,
    ModuleLevelInput,
    ModuleStat,
    ModuleTalentChange,
    Observation,
    analyze_modules,
)
from arknights_mcp.db.repositories.operators import OperatorRepository
from arknights_mcp.services.operators import (
    OperatorProvenance,
    cost_item_ids,
    dedup_and_label_changes,
    hoist_uniform_changes,
    hoist_uniform_template,
    pair_cost_item_names,
    shape_blackboard,
)
from arknights_mcp.util.coerce import json_load

#: Facts-only vs facts + deterministic module observations (mirrors the model).
CompareMode = Literal["facts_only", "with_observations"]

#: Typed outcome of a module comparison. The full §V23 status vocabulary is wired
#: into the tool envelope (§T29); this service reports only these two.
ModuleCompareStatus = Literal["ok", "not_found"]


@dataclass(frozen=True)
class ModuleLevelComparison:
    """One module's change bundle at one requested level (decoded JSON; §V18/§V16).

    ``present`` is ``False`` when the module does not define this level; the change
    fields are then ``None`` (absent, not an empty change).
    """

    level: int
    present: bool
    stat_bonus: object | None
    trait_changes: object | None
    talent_changes: object | None
    cost: object | None


@dataclass(frozen=True)
class ModuleComparison:
    """One module: metadata + its per-requested-level change bundles.

    ``trait_change_description`` is the in-game trait effect TEMPLATE hoisted once to the
    module when every level's trait change carries the identical template (§V66.3); it is
    ``None`` when the module carries no trait template or the templates differ across
    levels, in which case each level's ``trait_changes`` entry keeps its own. The hoist is
    byte-lossless -- the text lives in exactly one place.

    ``trait_changes`` / ``talent_changes`` hoist a WHOLE change bundle to the module when it
    is byte-identical across every present level (§V66.3/§V83) -- each present level then
    omits its copy; both are ``None`` when the bundle varies by level (or is absent), in
    which case each level keeps its own. (``trait_changes`` here carries no description: the
    trait template rides ``trait_change_description`` separately.) Byte-lossless.
    """

    game_id: str
    module_type: str | None
    display_name: str | None
    unlock_phase: int | None
    unlock_level: int | None
    trait_change_description: str | None
    trait_changes: object | None
    talent_changes: object | None
    levels: tuple[ModuleLevelComparison, ...]


@dataclass(frozen=True)
class ModuleCompareResult:
    """Domain result of :func:`compare_operator_modules`.

    Carries region + provenance (§V5) on an ``ok`` result. ``observations`` /
    ``warnings`` / ``analyzer_version`` are populated only in ``with_observations``
    mode. ``status == "not_found"`` implies ``provenance is None`` and empty modules.
    """

    status: ModuleCompareStatus
    server: str
    game_id: str
    operator_display_name: str | None
    levels: tuple[int, ...]
    mode: CompareMode
    modules: tuple[ModuleComparison, ...]
    observations: tuple[Observation, ...]
    warnings: tuple[str, ...]
    analyzer_version: str | None
    provenance: OperatorProvenance | None


def _stats(stat_bonus: object) -> tuple[ModuleStat, ...]:
    """Extract the typed ``(key, value)`` attribute pairs from a decoded stat bonus.

    Reads the allowlisted ``attributeBlackboard`` shape (``[{"key", "value"}, ...]``,
    §V18); a non-numeric or malformed entry is skipped rather than guessed at (§V26).
    ``bool`` is excluded explicitly (``isinstance(True, int)`` is ``True``).
    """
    if not isinstance(stat_bonus, list):
        return ()
    out: list[ModuleStat] = []
    for entry in stat_bonus:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        value = entry.get("value")
        if isinstance(key, str) and isinstance(value, int | float) and not isinstance(value, bool):
            out.append(ModuleStat(key=key, value=float(value)))
    return tuple(out)


def _talent_changes(talent_changes: object) -> tuple[ModuleTalentChange, ...]:
    """Extract the typed ``talentIndex`` of each decoded talent change (§V26)."""
    if not isinstance(talent_changes, list):
        return ()
    out: list[ModuleTalentChange] = []
    for entry in talent_changes:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("talentIndex")
        out.append(
            ModuleTalentChange(
                talent_index=idx if isinstance(idx, int) and not isinstance(idx, bool) else None
            )
        )
    return tuple(out)


def _trait_count(trait_changes: object) -> int:
    """How many trait override candidates a level carries (0 when absent; §V26)."""
    return len(trait_changes) if isinstance(trait_changes, list) else 0


def _change_descriptions(changes: object) -> list[str | None]:
    """The ``description`` template of each change bundle in a decoded list (§V66.3 input).

    A non-list value (the source carried no change at this level) yields an empty list, so
    a trait-less level does not inject a spurious ``None`` into the uniformity check.
    """
    if not isinstance(changes, list):
        return []
    return [b.get("description") for b in changes if isinstance(b, dict)]


def _strip_change_description(changes: object) -> object:
    """Drop the hoisted ``description`` from each bundle of a decoded change list (§V66.3).

    Applied to a level's shaped ``trait_changes`` only when the template was hoisted to the
    parent module; every other key (blackboard, unlock condition, potential rank) is
    preserved and a non-list value is returned unchanged.
    """
    if not isinstance(changes, list):
        return changes
    return [
        {k: v for k, v in b.items() if k != "description"} if isinstance(b, dict) else b
        for b in changes
    ]


def _not_found(
    server: str, game_id: str, levels: tuple[int, ...], mode: CompareMode
) -> ModuleCompareResult:
    return ModuleCompareResult(
        status="not_found",
        server=server,
        game_id=game_id,
        operator_display_name=None,
        levels=levels,
        mode=mode,
        modules=(),
        observations=(),
        warnings=(),
        analyzer_version=None,
        provenance=None,
    )


def compare_operator_modules(
    conn: sqlite3.Connection,
    *,
    server: str,
    game_id: str,
    levels: tuple[int, ...] = (1, 2, 3),
    mode: CompareMode = "facts_only",
) -> ModuleCompareResult:
    """Compare one operator's modules across ``levels`` for ``server`` (§T45; §V5/§V7).

    Read-only; parameterized SQL only (§V2). The operator is resolved by its unique
    ``(server, game_id)`` key, so an ``en`` operator is never surfaced under a ``cn``
    query (§V5); a missing operator returns ``status == "not_found"``. ``levels`` is
    deduped + sorted here defensively -- the bounded input model is the validation
    gate (subset of {1, 2, 3}, non-empty); this service stays graceful for a direct
    caller. In ``with_observations`` mode it runs the deterministic module analyzer
    (§T46) and returns its §V6 observations. Both transports call this (§V14).
    """
    requested = tuple(sorted(set(levels)))
    repo = OperatorRepository(conn)
    operator = repo.operator_by_game_id(server, game_id)
    if operator is None:
        return _not_found(server, game_id, requested, mode)

    # Materialize each module's level rows once, and pre-decode + collect the
    # upgrade-cost item ids across the requested levels so a single region-scoped
    # lookup resolves every cost name (§T132/§V69; en/cn never mixed, §V5).
    module_rows = [
        (module, {row.level: row for row in repo.module_levels(module.module_pk)})
        for module in repo.modules(operator.operator_pk)
    ]
    cost_by_key: dict[tuple[int, int], object] = {}
    cost_ids: set[str] = set()
    for module, rows in module_rows:
        for level in requested:
            row = rows.get(level)
            if row is not None:
                cost = json_load(row.cost_json)
                cost_by_key[(module.module_pk, level)] = cost
                cost_ids |= cost_item_ids(cost)
    item_names = repo.item_display_names(server, cost_ids)

    comparisons: list[ModuleComparison] = []
    analyzer_modules: list[ModuleInput] = []
    for module, rows in module_rows:
        # §V66.3: the trait effect TEMPLATE is byte-identical across a module's levels in
        # the common case, so hoist it once to the module (dropped from every per-level
        # trait_changes entry below) when every level agrees; keep it inline when the
        # templates differ so no text is lost. Computed over the raw decode before shaping.
        trait_change_description = hoist_uniform_template(
            desc
            for level in requested
            if (present_row := rows.get(level)) is not None
            for desc in _change_descriptions(json_load(present_row.trait_changes_json))
        )
        # Shape each requested level once; a present level's trait/talent change lists are
        # description-stripped (when the template was hoisted), then deduped + token-labelled
        # (§V83/B88). The analyzer reads the RAW decode (key/value/talentIndex), so it is
        # unaffected by this emit shaping.
        shaped_levels: list[tuple[int, bool, object, object, object, object]] = []
        analyzer_levels: list[ModuleLevelInput] = []
        for level in requested:
            row = rows.get(level)
            if row is None:
                shaped_levels.append((level, False, None, None, None, None))
                analyzer_levels.append(
                    ModuleLevelInput(
                        level=level,
                        present=False,
                        stats=(),
                        trait_change_count=0,
                        talent_changes=(),
                    )
                )
                continue
            stat_bonus = json_load(row.stat_bonus_json)
            trait_changes = json_load(row.trait_changes_json)
            talent_changes = json_load(row.talent_changes_json)
            # §T138/§V67/B63: drop always-null blackboard ``valueStr`` keys at emit.
            trait_shaped = shape_blackboard(trait_changes)
            # §V66.3: when the trait template was hoisted to the module, strip it from this
            # level's trait_changes so the text is emitted exactly once.
            if trait_change_description is not None:
                trait_shaped = _strip_change_description(trait_shaped)
            shaped_levels.append(
                (
                    level,
                    True,
                    shape_blackboard(stat_bonus),
                    dedup_and_label_changes(trait_shaped),
                    dedup_and_label_changes(shape_blackboard(talent_changes)),
                    # §T132/§V69: each {id,count,type} cost entry paired with its item
                    # display_name (additive, §V21); an un-named id is left as-is.
                    pair_cost_item_names(cost_by_key[(module.module_pk, level)], item_names),
                )
            )
            analyzer_levels.append(
                ModuleLevelInput(
                    level=level,
                    present=True,
                    stats=_stats(stat_bonus),
                    trait_change_count=_trait_count(trait_changes),
                    talent_changes=_talent_changes(talent_changes),
                )
            )
        # §V66.3/§V83: a change bundle byte-identical across every PRESENT level is hoisted
        # once to the module (dropped from each level below); ``None`` keeps it per level.
        trait_hoist = hoist_uniform_changes([t for _l, p, _s, t, _tal, _c in shaped_levels if p])
        talent_hoist = hoist_uniform_changes([tal for _l, p, _s, _t, tal, _c in shaped_levels if p])
        comp_levels = [
            ModuleLevelComparison(
                level=lvl,
                present=present,
                stat_bonus=stat,
                trait_changes=None if trait_hoist is not None else trait,
                talent_changes=None if talent_hoist is not None else talent,
                cost=cost,
            )
            for lvl, present, stat, trait, talent, cost in shaped_levels
        ]
        comparisons.append(
            ModuleComparison(
                game_id=module.game_id,
                module_type=module.module_type,
                display_name=module.display_name,
                unlock_phase=module.unlock_phase,
                unlock_level=module.unlock_level,
                trait_change_description=trait_change_description,
                trait_changes=trait_hoist,
                talent_changes=talent_hoist,
                levels=tuple(comp_levels),
            )
        )
        analyzer_modules.append(
            ModuleInput(
                game_id=module.game_id,
                module_type=module.module_type,
                display_name=module.display_name,
                levels=tuple(analyzer_levels),
            )
        )

    observations: tuple[Observation, ...] = ()
    warnings: tuple[str, ...] = ()
    analyzer_version: str | None = None
    if mode == "with_observations":
        analysis = analyze_modules(
            ModuleAnalysisContext(
                server=operator.server,
                operator_game_id=operator.game_id,
                requested_levels=requested,
                modules=tuple(analyzer_modules),
            )
        )
        observations = analysis.observations
        warnings = analysis.warnings
        analyzer_version = analysis.analyzer_version

    return ModuleCompareResult(
        status="ok",
        server=operator.server,
        game_id=operator.game_id,
        operator_display_name=operator.display_name,
        levels=requested,
        mode=mode,
        modules=tuple(comparisons),
        observations=observations,
        warnings=warnings,
        analyzer_version=analyzer_version,
        provenance=OperatorProvenance(
            snapshot_id=operator.snapshot_id, imported_at=operator.imported_at
        ),
    )
