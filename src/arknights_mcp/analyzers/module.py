"""Operator module analyzer.

Deterministic, evidence-backed observations about one operator's modules at the
requested potential levels. Pure and DB-free: the compare service decodes
the vetted structural JSON into the typed inputs below and calls
:func:`analyze_modules`; there is no natural-language input -- every rule reads
typed fields only, never a name or description string.

Each observation carries the five observation fields (``rule_id`` + evidence + confidence
+ limitations + ``analyzer_version``) reusing the shared
:class:`~arknights_mcp.analyzers.base.Observation` / ``EvidenceItem`` vocabulary.
Observations state capability facts (how a module's stat bonus scales across
its levels) -- never a "mandatory" / "best-in-slot" verdict; the raw per-level
bonuses live in the comparison rows, so the stat observation reports only the
cross-level change they do not spell out. A requested level a module does
not define is recorded as a warning, never concluded from.

One rule, not three (ADR 0018). ``module.trait_change`` and
``module.talent_change`` restated facts the response already carried beside them: the
levels that alter the trait, and which talent index each change targets, are both
visible in the ``trait_changes`` / ``talent_changes`` rows the same payload emits, so
6 of 9 observations on a three-module operator were re-readings rather than findings.
``module.stat_bonus`` survives because a cross-level DELTA is genuinely computed --
the rows carry absolute bonuses per level and never the step between them.
"""

from __future__ import annotations

from dataclasses import dataclass

from arknights_mcp.analyzers.base import ANALYZER_VERSION, EvidenceItem, Observation

_CATEGORY = "module"
#: Direct typed structural fields (attributeBlackboard / override bundles) drive
#: these observations, so confidence is high -- no inference is involved.
_CONFIDENCE = 0.9


@dataclass(frozen=True)
class ModuleStat:
    """One attribute bonus a module level grants (a typed ``key``/``value`` pair)."""

    key: str
    value: float


@dataclass(frozen=True)
class ModuleLevelInput:
    """One requested potential level of a module, already decoded to typed fields.

    ``present`` distinguishes "the module defines this level" from "the requested
    level is absent" -- an absent level carries no changes and is surfaced as a
    warning rather than concluded from as if it were empty.
    """

    level: int
    present: bool
    stats: tuple[ModuleStat, ...]


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
    """Aggregate result of running the module rules over one operator."""

    server: str
    operator_game_id: str
    observations: tuple[Observation, ...]
    warnings: tuple[str, ...]
    analyzer_version: str = ANALYZER_VERSION


def _label(module: ModuleInput) -> str:
    """A stable, typed handle for a module in a summary (never source prose).

    Prefers the recognizable ``module_type`` (e.g. ``"CX-1"``) and falls back to
    the ``game_id``; the allowlisted ``display_name`` is a proper name, not prose,
    but the type/id keeps summaries deterministic and language-neutral.
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
    """How a module's attribute bonuses CHANGE across its levels.

    The per-level absolute bonuses already sit in the comparison's ``stat_bonus`` rows, so
    restating them adds nothing (evidence refs the facts, never copies them). This
    computes the cross-level delta the raw rows do not spell out -- each stat's step change
    between consecutive present levels that both define it. ``None`` when no stat changes
    across two present levels: a single-level bonus is fully visible in its own row, so it
    needs no observation, and an absent level is never treated as a zero.
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
            if delta == 0:
                # A stat constant across two present levels is not a change; emitting a
                # "+0" step would restate a non-change as a change. Skip it
                # so an all-constant module falls through to `not evidence` -> None.
                continue
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


def _analyze_module(module: ModuleInput) -> tuple[list[Observation], list[str]]:
    """Run every module rule over one module; collect observations + warnings."""
    observations = [obs for obs in (_stat_observation(module),) if obs is not None]
    # A requested level the module does not define is omitted from the comparison
    # and warned, never concluded from as an empty/zero change.
    warnings = [
        f"module {module.game_id}: requested level {level.level} is not defined; "
        "omitted from the comparison"
        for level in module.levels
        if not level.present
    ]
    return observations, warnings


def analyze_modules(ctx: ModuleAnalysisContext) -> ModuleAnalysis:
    """Run the deterministic module rules over ``ctx``.

    Modules are processed in the order supplied (the compare service orders them by
    ``game_id``), so observations + warnings are emitted deterministically. Every
    observation carries the five observation fields; the analyzer adds no prescriptive
    language.
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
