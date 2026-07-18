"""Operator module analyzer (§T46; §V6, §V7, §V26).

Deterministic, evidence-backed observations about one operator's modules at the
requested potential levels. Pure and DB-free: the compare service (§T45) decodes
the vetted structural JSON (§V18) into the typed inputs below and calls
:func:`analyze_modules`; there is no natural-language input -- every rule reads
typed fields only (§V26), never a name or description string.

Each observation carries the five §V6 fields (``rule_id`` + evidence + confidence
+ limitations + ``analyzer_version``) reusing the shared
:class:`~arknights_mcp.analyzers.base.Observation` / ``EvidenceItem`` vocabulary
(§V37). Observations state capability facts (what a module changes, by how much,
at which level) -- never a "mandatory" / "best-in-slot" verdict (§V7). A requested
level a module does not define is recorded as a §V26 warning, never concluded from.
"""

from __future__ import annotations

from dataclasses import dataclass

from arknights_mcp.analyzers.base import ANALYZER_VERSION, EvidenceItem, Observation

_CATEGORY = "module"
#: Direct typed structural fields (attributeBlackboard / override bundles) drive
#: these observations, so confidence is high -- no inference is involved (§V6).
_CONFIDENCE = 0.9


@dataclass(frozen=True)
class ModuleStat:
    """One attribute bonus a module level grants (a typed ``key``/``value`` pair)."""

    key: str
    value: float


@dataclass(frozen=True)
class ModuleTalentChange:
    """One talent a module level adds or overrides (by typed index; no prose)."""

    talent_index: int | None


@dataclass(frozen=True)
class ModuleLevelInput:
    """One requested potential level of a module, already decoded to typed fields.

    ``present`` distinguishes "the module defines this level" from "the requested
    level is absent" -- an absent level carries no changes and is surfaced as a
    §V26 warning rather than concluded from as if it were empty.
    """

    level: int
    present: bool
    stats: tuple[ModuleStat, ...]
    trait_change_count: int
    talent_changes: tuple[ModuleTalentChange, ...]


@dataclass(frozen=True)
class ModuleInput:
    """One operator module + its requested levels (rule input)."""

    game_id: str
    module_type: str | None
    display_name: str | None
    levels: tuple[ModuleLevelInput, ...]


@dataclass(frozen=True)
class ModuleAnalysisContext:
    """Typed input to the module analyzer for one operator's modules."""

    server: str
    operator_game_id: str
    requested_levels: tuple[int, ...]
    modules: tuple[ModuleInput, ...]


@dataclass(frozen=True)
class ModuleAnalysis:
    """Aggregate result of running the module rules over one operator (§V6)."""

    server: str
    operator_game_id: str
    observations: tuple[Observation, ...]
    warnings: tuple[str, ...]
    analyzer_version: str = ANALYZER_VERSION


def _label(module: ModuleInput) -> str:
    """A stable, typed handle for a module in a summary (never source prose).

    Prefers the recognizable ``module_type`` (e.g. ``"CX-1"``) and falls back to
    the ``game_id``; the allowlisted ``display_name`` is a proper name, not prose,
    but the type/id keeps summaries deterministic and language-neutral (§V26).
    """
    return module.module_type or module.game_id


def _stat_summary(by_key: dict[str, list[tuple[int, float]]]) -> str:
    """Render the per-stat progression factually: ``atk: 34 (Lv1), 48 (Lv2)``.

    No sign is forced onto a value -- ``{:g}`` shows a negative naturally, so a
    module that trades a stat down is stated correctly rather than as ``+-5``.
    """
    parts: list[str] = []
    for key in sorted(by_key):
        points = ", ".join(f"{value:g} (Lv{level})" for level, value in by_key[key])
        parts.append(f"{key}: {points}")
    return "; ".join(parts)


def _stat_observation(module: ModuleInput) -> Observation | None:
    """Observation of a module's attribute bonuses across the present levels (§V6).

    ``None`` when no present level grants a typed stat -- an absent bonus is not
    reported as a zero bonus (§V26).
    """
    evidence: list[EvidenceItem] = []
    by_key: dict[str, list[tuple[int, float]]] = {}
    for level in module.levels:
        if not level.present:
            continue
        for stat in level.stats:
            evidence.append(
                EvidenceItem(
                    ref=module.game_id,
                    field=f"stat_bonus.{stat.key}",
                    value=stat.value,
                    note=f"module level {level.level}",
                )
            )
            by_key.setdefault(stat.key, []).append((level.level, stat.value))
    if not evidence:
        return None
    label = _label(module)
    return Observation(
        rule_id="module.stat_bonus",
        category=_CATEGORY,
        tag="stat_bonus",
        title="Module attribute bonuses",
        summary=f"{label} module grants attribute bonuses -- {_stat_summary(by_key)}.",
        confidence=_CONFIDENCE,
        evidence=tuple(evidence),
        limitations=(),
    )


def _trait_observation(module: ModuleInput) -> Observation | None:
    """Observation of the levels at which a module alters the operator's trait (§V6)."""
    evidence = [
        EvidenceItem(
            ref=module.game_id,
            field="trait_changes",
            value=level.trait_change_count,
            note=f"module level {level.level}",
        )
        for level in module.levels
        if level.present and level.trait_change_count > 0
    ]
    if not evidence:
        return None
    levels = ", ".join(str(e.note).removeprefix("module level ") for e in evidence)
    return Observation(
        rule_id="module.trait_change",
        category=_CATEGORY,
        tag="trait_change",
        title="Module alters operator trait",
        summary=f"{_label(module)} module alters the operator's base trait at level(s) {levels}.",
        confidence=_CONFIDENCE,
        evidence=tuple(evidence),
        limitations=(),
    )


def _talent_observation(module: ModuleInput) -> Observation | None:
    """Observation of the talents a module adds/overrides, by typed index (§V6)."""
    evidence: list[EvidenceItem] = []
    indices: set[int] = set()
    for level in module.levels:
        if not level.present:
            continue
        for change in level.talent_changes:
            evidence.append(
                EvidenceItem(
                    ref=module.game_id,
                    field="talent_changes.talentIndex",
                    value=change.talent_index,
                    note=f"module level {level.level}",
                )
            )
            if change.talent_index is not None:
                indices.add(change.talent_index)
    if not evidence:
        return None
    named = "talent(s) " + ", ".join(str(i) for i in sorted(indices)) if indices else "a talent"
    return Observation(
        rule_id="module.talent_change",
        category=_CATEGORY,
        tag="talent_change",
        title="Module adds or overrides a talent",
        summary=f"{_label(module)} module adds or enhances {named}.",
        confidence=_CONFIDENCE,
        evidence=tuple(evidence),
        limitations=(),
    )


def _analyze_module(module: ModuleInput) -> tuple[list[Observation], list[str]]:
    """Run every module rule over one module; collect observations + §V26 warnings."""
    observations = [
        obs
        for obs in (
            _stat_observation(module),
            _trait_observation(module),
            _talent_observation(module),
        )
        if obs is not None
    ]
    # A requested level the module does not define is omitted from the comparison
    # and warned, never concluded from as an empty/zero change (§V26).
    warnings = [
        f"module {module.game_id}: requested level {level.level} is not defined; "
        "omitted from the comparison"
        for level in module.levels
        if not level.present
    ]
    return observations, warnings


def analyze_modules(ctx: ModuleAnalysisContext) -> ModuleAnalysis:
    """Run the deterministic module rules over ``ctx`` (§V6, §V26).

    Modules are processed in the order supplied (the compare service orders them by
    ``game_id``), so observations + warnings are emitted deterministically. Every
    observation carries the five §V6 fields; the analyzer adds no prescriptive
    language (§V7).
    """
    observations: list[Observation] = []
    warnings: list[str] = []
    for module in ctx.modules:
        obs, warns = _analyze_module(module)
        observations.extend(obs)
        warnings.extend(warns)
    return ModuleAnalysis(
        server=ctx.server,
        operator_game_id=ctx.operator_game_id,
        observations=tuple(observations),
        warnings=tuple(warnings),
    )
