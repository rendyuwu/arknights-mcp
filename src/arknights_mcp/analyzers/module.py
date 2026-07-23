"""Operator module analyzer (§T46; §V6, §V7, §V26).

Deterministic, evidence-backed observations about one operator's modules at the
requested potential levels. Pure and DB-free: the compare service (§T45) decodes
the vetted structural JSON (§V18) into the typed inputs below and calls
:func:`analyze_modules`; there is no natural-language input -- every rule reads
typed fields only (§V26), never a name or description string.

Each observation carries the five §V6 fields (``rule_id`` + evidence + confidence
+ limitations + ``analyzer_version``) reusing the shared
:class:`~arknights_mcp.analyzers.base.Observation` / ``EvidenceItem`` vocabulary
(§V37). Observations state capability facts (which talents a module changes and how
its stat bonus scales across levels) -- never a "mandatory" / "best-in-slot" verdict
(§V7); the raw per-level bonuses live in the comparison rows, so the stat observation
reports only the cross-level change they do not spell out (§V66.1). A requested
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


def _stat_diff_summary(by_key: dict[str, list[tuple[int, int, float]]]) -> str:
    """Render each stat's per-step cross-level change: ``atk: +14 (Lv1->2), +18 (Lv2->3)``.

    The sign is forced (``{:+g}``) so a raise reads ``+14`` and a downward trade reads
    ``-5`` -- the change, not the absolute value already visible in the stat_bonus rows.
    """
    parts: list[str] = []
    for key in sorted(by_key):
        steps = ", ".join(f"{delta:+g} (Lv{a}->{b})" for a, b, delta in by_key[key])
        parts.append(f"{key}: {steps}")
    return "; ".join(parts)


def _stat_observation(module: ModuleInput) -> Observation | None:
    """How a module's attribute bonuses CHANGE across its levels (§V6, §V66.1).

    The per-level absolute bonuses already sit in the comparison's ``stat_bonus`` rows, so
    restating them adds nothing (§V66.1: evidence refs the facts, never copies them). This
    computes the cross-level delta the raw rows do not spell out -- each stat's step change
    between consecutive present levels that both define it. ``None`` when no stat changes
    across two present levels: a single-level bonus is fully visible in its own row, so it
    needs no observation, and an absent level is never treated as a zero (§V26).
    """
    present = [
        (level.level, {stat.key: stat.value for stat in level.stats})
        for level in module.levels
        if level.present
    ]
    evidence: list[EvidenceItem] = []
    by_key: dict[str, list[tuple[int, int, float]]] = {}
    for (level_a, stats_a), (level_b, stats_b) in zip(present, present[1:], strict=False):
        for key in sorted(stats_a.keys() & stats_b.keys()):
            delta = stats_b[key] - stats_a[key]
            evidence.append(
                EvidenceItem(
                    ref=module.game_id,
                    field=f"stat_bonus.{key}",
                    value=delta,
                    note=f"module level {level_a} to {level_b}",
                )
            )
            by_key.setdefault(key, []).append((level_a, level_b, delta))
    if not evidence:
        return None
    return Observation(
        rule_id="module.stat_bonus",
        category=_CATEGORY,
        tag="stat_bonus",
        title="Module attribute bonus change across levels",
        summary=f"{_label(module)} module attribute bonus changes across levels -- "
        f"{_stat_diff_summary(by_key)}.",
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
    """Observation of the talents a module adds/overrides, by typed index (§V6, §V71).

    A ``talentIndex`` of ``-1`` is the game-data convention for the operator's TOKEN /
    summon effect, not a numbered operator talent; it is glossed as "the token effect"
    rather than emitted as a bare ``-1`` -- an internal convention never reaches the
    client (§V71), in the summary or the evidence. Numbered talents (index >= 0) are
    named by their index; an absent index is a generic "a talent" (§V26 invents none).
    """
    evidence: list[EvidenceItem] = []
    indices: set[int] = set()
    token_effect = False
    for level in module.levels:
        if not level.present:
            continue
        for change in level.talent_changes:
            idx = change.talent_index
            if idx is not None and idx < 0:
                # -1 = the operator's token/summon effect; a typed flag, never a bare -1.
                token_effect = True
                evidence.append(
                    EvidenceItem(
                        ref=module.game_id,
                        field="talent_changes.token_effect",
                        value=True,
                        note=f"module level {level.level}",
                    )
                )
                continue
            evidence.append(
                EvidenceItem(
                    ref=module.game_id,
                    field="talent_changes.talentIndex",
                    value=idx,
                    note=f"module level {level.level}",
                )
            )
            if idx is not None:
                indices.add(idx)
    if not evidence:
        return None
    phrases: list[str] = []
    if indices:
        phrases.append("talent(s) " + ", ".join(str(i) for i in sorted(indices)))
    if token_effect:
        phrases.append("the token effect")
    named = " and ".join(phrases) if phrases else "a talent"
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
