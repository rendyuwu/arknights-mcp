"""Internal operator intel service (§T44): the single domain entry point both
transports call to fetch one operator's facts (§V14).

Given a read-only SQLite connection and a ``(server, game_id)`` selector, it loads
the operator's typed facts + region + provenance (§V5). The heavy sections
(phases, skills, talents, modules) are opt-in: each is loaded only when its include
flag is set, so the default response stays small (§V22); a lightweight ``summary``
(core identity + per-section counts) and the region provenance are always available.
The service adds no natural-language interpretation of its own -- it emits only
typed, vetted fields; the stored structural JSON was allowlisted + sanitized at
import (§V18/§V31) and is decoded here (never prose, §V16).

Read-only + parameterized SQL only (§V2): the parameterized ``SELECT``s live in
:class:`~arknights_mcp.db.repositories.operators.OperatorRepository` (§T20), the
sole sanctioned SQL surface; this service only reads through it and never mutates
the database. It does not open the connection (callers pass one in), so both
transports share this exact function (§V14). No transport-specific logic lives here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from arknights_mcp.db.repositories.operators import (
    ModuleLevelRow,
    ModuleRow,
    OperatorPhaseRow,
    OperatorRepository,
    OperatorRow,
    OperatorSectionCounts,
    OperatorSkillRow,
    SkillLevelRow,
    TalentLevelRow,
    TalentRow,
)
from arknights_mcp.util.coerce import json_load

#: Typed outcome of an operator lookup. The full §V23 status vocabulary is wired
#: into the tool envelope (§T29); this service reports only these two.
OperatorLookupStatus = Literal["ok", "not_found"]


@dataclass(frozen=True)
class OperatorProvenance:
    """Region-scoped provenance for a factual operator response (§V5)."""

    snapshot_id: str
    imported_at: str


@dataclass(frozen=True)
class OperatorSummary:
    """Compact identity + per-section counts (always-on by default; §V22).

    Lets a client see the core operator identity and *what heavy sections exist*
    (how many phases/skills/talents/modules) without pulling the sections
    themselves -- they stay opt-in via the include flags.
    """

    rarity: int | None
    profession: str | None
    subclass_id: str | None
    position: str | None
    tags: tuple[str, ...]
    obtainable: bool
    phase_count: int
    skill_count: int
    talent_count: int
    module_count: int


@dataclass(frozen=True)
class OperatorPhaseFacts:
    """One elite phase's typed stat block (no prose; §V16/§V18)."""

    phase: int
    max_level: int | None
    max_hp: int | None
    atk: int | None
    def_: int | None
    res: int | None
    redeploy_time: int | None
    cost: int | None
    block_count: int | None
    attack_interval: float | None
    range_id: str | None


@dataclass(frozen=True)
class SkillLevelFacts:
    """One mastery level of a skill; ``blackboard`` decoded from vetted JSON (§V18)."""

    level: int
    sp_cost: int | None
    initial_sp: int | None
    duration: float | None
    range_id: str | None
    blackboard: object | None


@dataclass(frozen=True)
class OperatorSkillFacts:
    """One skill slot: metadata + its ordered mastery levels."""

    game_id: str
    display_name: str | None
    skill_type: str | None
    sp_type: str | None
    duration_type: str | None
    slot_index: int
    unlock_phase: int | None
    unlock_level: int | None
    levels: tuple[SkillLevelFacts, ...]


@dataclass(frozen=True)
class TalentVariantFacts:
    """One talent variant (potential/phase gated); ``blackboard`` decoded (§V18)."""

    variant_index: int
    unlock_phase: int | None
    unlock_level: int | None
    potential_rank: int | None
    blackboard: object | None


@dataclass(frozen=True)
class OperatorTalentFacts:
    """One talent: short label + its ordered variants."""

    talent_index: int
    display_name: str | None
    variants: tuple[TalentVariantFacts, ...]


@dataclass(frozen=True)
class ModuleLevelFacts:
    """One module level: the numeric change bundles decoded from vetted JSON (§V18)."""

    level: int
    stat_bonus: object | None
    trait_changes: object | None
    talent_changes: object | None
    cost: object | None


@dataclass(frozen=True)
class OperatorModuleFacts:
    """One module: metadata + its ordered levels."""

    game_id: str
    module_type: str | None
    display_name: str | None
    unlock_phase: int | None
    unlock_level: int | None
    levels: tuple[ModuleLevelFacts, ...]


@dataclass(frozen=True)
class OperatorFacts:
    """Typed, allowlisted facts about one operator (no prose; §V16/§V18).

    Carries region (``server``) + provenance (§V5). ``summary`` and the heavy
    sections are populated per the include flags (an omitted section is an empty
    tuple; an omitted summary is ``None``); ``provenance`` is always present so a
    fact always carries its region attribution (§V5).
    """

    server: str
    game_id: str
    display_name: str | None
    summary: OperatorSummary | None
    phases: tuple[OperatorPhaseFacts, ...]
    skills: tuple[OperatorSkillFacts, ...]
    talents: tuple[OperatorTalentFacts, ...]
    modules: tuple[OperatorModuleFacts, ...]
    provenance: OperatorProvenance


@dataclass(frozen=True)
class OperatorDetailResult:
    """Domain result of :func:`get_operator`.

    ``status == "not_found"`` implies ``operator is None``. An ``ok`` result carries
    region + provenance on ``operator`` (§V5).
    """

    status: OperatorLookupStatus
    server: str
    operator: OperatorFacts | None


def _tags(tag_json: str | None) -> tuple[str, ...]:
    """Decode the stored ``tag_json`` (allowlisted + sanitized at import) to strings."""
    decoded = json_load(tag_json)
    if not isinstance(decoded, list):
        return ()
    return tuple(t for t in decoded if isinstance(t, str))


def _summary(operator: OperatorRow, counts: OperatorSectionCounts) -> OperatorSummary:
    return OperatorSummary(
        rarity=operator.rarity,
        profession=operator.profession,
        subclass_id=operator.subclass_id,
        position=operator.position,
        tags=_tags(operator.tag_json),
        obtainable=operator.obtainable,
        phase_count=counts.phases,
        skill_count=counts.skills,
        talent_count=counts.talents,
        module_count=counts.modules,
    )


def _phase_facts(row: OperatorPhaseRow) -> OperatorPhaseFacts:
    return OperatorPhaseFacts(
        phase=row.phase,
        max_level=row.max_level,
        max_hp=row.max_hp,
        atk=row.atk,
        def_=row.def_,
        res=row.res,
        redeploy_time=row.redeploy_time,
        cost=row.cost,
        block_count=row.block_count,
        attack_interval=row.attack_interval,
        range_id=row.range_id,
    )


def _skill_facts(repo: OperatorRepository, row: OperatorSkillRow) -> OperatorSkillFacts:
    return OperatorSkillFacts(
        game_id=row.game_id,
        display_name=row.display_name,
        skill_type=row.skill_type,
        sp_type=row.sp_type,
        duration_type=row.duration_type,
        slot_index=row.slot_index,
        unlock_phase=row.unlock_phase,
        unlock_level=row.unlock_level,
        levels=tuple(_skill_level_facts(lv) for lv in repo.skill_levels(row.skill_pk)),
    )


def _skill_level_facts(row: SkillLevelRow) -> SkillLevelFacts:
    return SkillLevelFacts(
        level=row.level,
        sp_cost=row.sp_cost,
        initial_sp=row.initial_sp,
        duration=row.duration,
        range_id=row.range_id,
        blackboard=json_load(row.blackboard_json),
    )


def _talent_facts(repo: OperatorRepository, row: TalentRow) -> OperatorTalentFacts:
    return OperatorTalentFacts(
        talent_index=row.talent_index,
        display_name=row.display_name,
        variants=tuple(_talent_variant_facts(v) for v in repo.talent_levels(row.talent_pk)),
    )


def _talent_variant_facts(row: TalentLevelRow) -> TalentVariantFacts:
    return TalentVariantFacts(
        variant_index=row.variant_index,
        unlock_phase=row.unlock_phase,
        unlock_level=row.unlock_level,
        potential_rank=row.potential_rank,
        blackboard=json_load(row.blackboard_json),
    )


def _module_facts(repo: OperatorRepository, row: ModuleRow) -> OperatorModuleFacts:
    return OperatorModuleFacts(
        game_id=row.game_id,
        module_type=row.module_type,
        display_name=row.display_name,
        unlock_phase=row.unlock_phase,
        unlock_level=row.unlock_level,
        levels=tuple(_module_level_facts(lv) for lv in repo.module_levels(row.module_pk)),
    )


def _module_level_facts(row: ModuleLevelRow) -> ModuleLevelFacts:
    return ModuleLevelFacts(
        level=row.level,
        stat_bonus=json_load(row.stat_bonus_json),
        trait_changes=json_load(row.trait_changes_json),
        talent_changes=json_load(row.talent_changes_json),
        cost=json_load(row.cost_json),
    )


def get_operator(
    conn: sqlite3.Connection,
    *,
    server: str,
    game_id: str,
    include_summary: bool = True,
    include_phases: bool = False,
    include_skills: bool = False,
    include_talents: bool = False,
    include_modules: bool = False,
) -> OperatorDetailResult:
    """Fetch one operator's facts + opt-in heavy sections for ``server`` (§T44; §V5/§V23).

    Read-only; parameterized SQL only (§V2). The operator is resolved by its unique
    ``(server, game_id)`` key, so an ``en`` operator is never surfaced under a ``cn``
    query (§V5). A missing operator returns ``status == "not_found"`` (the tool maps
    it to the typed §V23 envelope). Heavy sections load only when their include flag
    is set, keeping the default response small (§V22). Both transports call this
    function (§V14).
    """
    repo = OperatorRepository(conn)
    operator = repo.operator_by_game_id(server, game_id)
    if operator is None:
        return OperatorDetailResult(status="not_found", server=server, operator=None)

    summary = (
        _summary(operator, repo.section_counts(operator.operator_pk)) if include_summary else None
    )
    phases = (
        tuple(_phase_facts(p) for p in repo.phases(operator.operator_pk)) if include_phases else ()
    )
    skills = (
        tuple(_skill_facts(repo, s) for s in repo.skills(operator.operator_pk))
        if include_skills
        else ()
    )
    talents = (
        tuple(_talent_facts(repo, t) for t in repo.talents(operator.operator_pk))
        if include_talents
        else ()
    )
    modules = (
        tuple(_module_facts(repo, m) for m in repo.modules(operator.operator_pk))
        if include_modules
        else ()
    )
    facts = OperatorFacts(
        server=operator.server,
        game_id=operator.game_id,
        display_name=operator.display_name,
        summary=summary,
        phases=phases,
        skills=skills,
        talents=talents,
        modules=modules,
        provenance=OperatorProvenance(
            snapshot_id=operator.snapshot_id, imported_at=operator.imported_at
        ),
    )
    return OperatorDetailResult(status="ok", server=operator.server, operator=facts)
