"""Internal operator intel service: the single domain entry point both
transports call to fetch one operator's facts.

Given a read-only SQLite connection and a ``(server, game_id)`` selector, it loads
the operator's typed facts + region + provenance. The heavy sections
(phases, skills, talents, modules) are opt-in: each is loaded only when its include
flag is set, so the default response stays small; a lightweight ``summary``
(core identity + per-section counts) and the region provenance are always available.
The service adds no natural-language interpretation of its own -- it emits only
typed, vetted fields; the stored structural JSON was allowlisted + sanitized at
import and is decoded here (never prose).

Read-only + parameterized SQL only: the parameterized ``SELECT``s live in
:class:`~arknights_mcp.db.repositories.operators.OperatorRepository`, the
sole sanctioned SQL surface; this service only reads through it and never mutates
the database. It does not open the connection (callers pass one in), so both
transports share this exact function. No transport-specific logic lives here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from arknights_mcp.db.repositories.operators import (
    BaseSkillRow,
    FactionRow,
    ModuleLevelRow,
    ModuleRow,
    OperatorPhaseRow,
    OperatorRepository,
    OperatorRow,
    OperatorSectionCounts,
    OperatorSkillRow,
    OperatorSkinRow,
    SkillLevelRow,
    TalentLevelRow,
    TalentRow,
)
from arknights_mcp.db.repositories.ranges import RangeRepository
from arknights_mcp.services.effect_changes import (
    dedup_and_label_changes,
    hoist_uniform_changes,
)
from arknights_mcp.services.range_grid import range_grid_rows
from arknights_mcp.util.coerce import json_load, uniform_str

#: Typed outcome of an operator lookup. The full status vocabulary is wired
#: into the tool envelope; this service reports only these two.
OperatorLookupStatus = Literal["ok", "not_found"]


@dataclass(frozen=True)
class OperatorProvenance:
    """Region-scoped provenance for a factual operator response."""

    snapshot_id: str
    imported_at: str


@dataclass(frozen=True)
class OperatorSummary:
    """Compact identity + per-section counts (always-on by default).

    Lets a client see the core operator identity and *what heavy sections exist*
    (how many phases/skills/talents/modules) without pulling the sections
    themselves -- they stay opt-in via the include flags.
    """

    rarity: int | None
    profession: str | None
    subclass_id: str | None
    #: The subclass id's display name. ``None`` when this build's
    #: ``uniequip_table`` carries no entry for the id -- the id still ships, and the
    #: absence is disclosed rather than papered over with a guess.
    subclass_name: str | None
    position: str | None
    tags: tuple[str, ...]
    obtainable: bool
    phase_count: int
    skill_count: int
    talent_count: int
    module_count: int
    #: Factions (ADR 0021), main first; empty on a build without them.
    factions: tuple[FactionRow, ...]
    #: The collab flag; ``None`` when this build does not know it.
    collab: bool | None


@dataclass(frozen=True)
class OperatorPhaseFacts:
    """One elite phase's typed stat block (no prose)."""

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
    """One mastery level of a skill; ``blackboard`` decoded from vetted JSON.

    ``description`` is the imported in-game effect TEMPLATE (mechanic text referencing
    the blackboard keys; ADR 0010), emitted alongside the blackboard for
    grounding. When the template is byte-identical across every one of the skill's
    levels it is hoisted once to the parent :class:`OperatorSkillFacts` and this
    per-level field is ``None`` (payload dedup); it is populated here only when
    the levels' templates differ, so the varying text is never lost.

    ``display_name`` / ``skill_type`` / ``sp_type`` / ``duration_type`` follow the same
    rule for the four fields the source scopes per level: ``None`` while the
    skill's own value applies to every level, populated when the levels disagree and the
    skill's is therefore absent.
    """

    level: int
    sp_cost: int | None
    initial_sp: int | None
    duration: float | None
    range_id: str | None
    blackboard: object | None
    description: str | None
    display_name: str | None
    skill_type: str | None
    sp_type: str | None
    duration_type: str | None


@dataclass(frozen=True)
class OperatorSkillFacts:
    """One skill slot: metadata + its ordered mastery levels.

    ``description`` is the in-game effect TEMPLATE hoisted once to the skill when it is
    byte-identical across all the skill's levels; it is ``None`` when the
    template varies by level, in which case each :class:`SkillLevelFacts` carries its
    own. The hoist is byte-lossless -- exactly one of the skill-level or per-level
    ``description`` carries the text.

    ``display_name`` / ``skill_type`` / ``sp_type`` / ``duration_type`` read the same way:
    the source scopes all four per level, so a value here is one every level
    shares, and ``None`` means either the levels disagree -- their own values are on the
    :class:`SkillLevelFacts` -- or the source carried none at all.
    """

    game_id: str
    display_name: str | None
    skill_type: str | None
    sp_type: str | None
    duration_type: str | None
    slot_index: int
    unlock_phase: int | None
    unlock_level: int | None
    description: str | None
    levels: tuple[SkillLevelFacts, ...]


@dataclass(frozen=True)
class TalentVariantFacts:
    """One talent variant (potential/phase gated); ``blackboard`` decoded.

    ``description`` is the imported in-game effect TEMPLATE (mechanic text referencing
    the blackboard keys; ADR 0010), emitted alongside the blackboard for
    grounding; ``None`` when the candidate carries none.
    """

    variant_index: int
    unlock_phase: int | None
    unlock_level: int | None
    potential_rank: int | None
    blackboard: object | None
    description: str | None


@dataclass(frozen=True)
class OperatorTalentFacts:
    """One talent: short label + its ordered variants."""

    talent_index: int
    display_name: str | None
    variants: tuple[TalentVariantFacts, ...]


@dataclass(frozen=True)
class ModuleLevelFacts:
    """One module level: the numeric change bundles decoded from vetted JSON."""

    level: int
    stat_bonus: object | None
    trait_changes: object | None
    talent_changes: object | None
    cost: object | None


@dataclass(frozen=True)
class OperatorModuleFacts:
    """One module: metadata + its ordered levels.

    ``trait_changes`` / ``talent_changes`` are the per-level change bundle hoisted once to
    the module when it is byte-identical across every level -- the client
    reads it as "the same at every level" and each level then omits its copy; both are
    ``None`` when the bundle varies by level (or is absent), in which case each level keeps
    its own. The hoist is byte-lossless -- the bundle lives in exactly one place.
    """

    game_id: str
    module_type: str | None
    display_name: str | None
    unlock_phase: int | None
    unlock_level: int | None
    trait_changes: object | None
    talent_changes: object | None
    levels: tuple[ModuleLevelFacts, ...]


@dataclass(frozen=True)
class OperatorSkinFacts:
    """One imported named skin (ADR 0015).

    ``is_alt_form`` is True when the skin's art belongs to an alternate playable
    form of the operator (Amiya family): the imported ``tmpl_id`` names that form
    while ``char_id`` stays the base operator's id, so the discriminator is
    ``tmpl_id != char_id``. ``portrait_id`` is the stem the mirror URL is
    derived from at emit time (never stored as a URL).
    """

    skin_id: str
    portrait_id: str
    display_name: str | None
    skin_group_id: str | None
    skin_group_name: str | None
    is_buy_skin: bool | None
    is_alt_form: bool


@dataclass(frozen=True)
class RangeGridFacts:
    """One resolved attack-range grid referenced by this operator.

    ``grids`` is the imported fact -- deploy-tile-relative ``(row, col)`` offsets --
    and ``rows`` is the same grid laid out as a readable board, empty
    only when the frame was refused by the size ceiling.
    """

    range_id: str
    grids: tuple[tuple[int, int], ...]
    rows: tuple[str, ...]


@dataclass(frozen=True)
class OperatorFacts:
    """Typed, allowlisted facts about one operator (no prose).

    Carries region (``server``) + provenance. ``summary`` and the heavy
    sections are populated per the include flags (an omitted section is an empty
    tuple; an omitted summary is ``None``); ``provenance`` is always present so a
    fact always carries its region attribution. ``skins`` is loaded only for
    the image-ref-emitting wiring (``load_skins``) and is empty on a build
    without the skin domain.

    ``ranges`` resolves every ``range_id`` the loaded phases/skills actually emit, and
    ``unresolved_range_ids`` names the ones this build has no grid for -- the two
    arms, both computed here so the transport only renders them.
    """

    server: str
    game_id: str
    display_name: str | None
    summary: OperatorSummary | None
    phases: tuple[OperatorPhaseFacts, ...]
    skills: tuple[OperatorSkillFacts, ...]
    talents: tuple[OperatorTalentFacts, ...]
    modules: tuple[OperatorModuleFacts, ...]
    skins: tuple[OperatorSkinFacts, ...]
    ranges: tuple[RangeGridFacts, ...]
    #: Base (RIIC) skill stages (ADR 0021), loaded by ``include_base_skills``.
    base_skills: tuple[BaseSkillRow, ...]
    #: ``include_base_skills`` was asked on a build predating migration 0021, so the
    #: empty ``base_skills`` means "no data", not "no base skills".
    base_skill_domain_missing: bool
    unresolved_range_ids: tuple[str, ...]
    provenance: OperatorProvenance


@dataclass(frozen=True)
class OperatorDetailResult:
    """Domain result of :func:`get_operator`.

    ``status == "not_found"`` implies ``operator is None``. An ``ok`` result carries
    region + provenance on ``operator``.
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


#: Optional keys omitted when their value is ``null`` (null discipline): a blackboard
#: entry's ``valueStr`` (null for ~60 numeric params on a full operator) and a
#: trait/talent-change bundle's effect ``description`` template (null when the source carried
#: no template -- e.g. a module's -1 token-effect talent change). Never emit ``null``
#: for these so a client is not forced to decide "none vs unknown"; omission is additive.
_NULL_OMIT_KEYS: frozenset[str] = frozenset({"valueStr", "description"})


def shape_blackboard(value: object) -> object:
    """Omit always-null optional keys from a decoded blackboard structure.

    Drops a ``valueStr`` / ``description`` key whose value is ``null`` (:data:`_NULL_OMIT_KEYS`)
    rather than emit ``null`` so the client is not forced to decide "none vs unknown".
    Recurses through the decoded dict/list structure so a ``blackboard`` nested inside a
    trait/talent-change bundle is cleaned too, and a bundle's null effect ``description``
    (a module -1 token-effect change carries none) is dropped; a *non-null* value (a real
    string param or an imported template) is kept, and every other key/shape is preserved
    exactly -- omitting an absent-optional key is additive/backward-compatible. The
    single home shared by the operator + module-compare read services.
    """
    if isinstance(value, dict):
        return {
            k: shape_blackboard(v)
            for k, v in value.items()
            if not (k in _NULL_OMIT_KEYS and v is None)
        }
    if isinstance(value, list):
        return [shape_blackboard(item) for item in value]
    return value


def hoist_uniform_template(values: Iterable[str | None]) -> str | None:
    """The single effect TEMPLATE shared by every element of ``values``, else ``None``.

    Returns the string when ``values`` collapses to exactly one distinct, non-``None``
    template -- the per-level byte-identical case this hoist targets, where the template is
    hoisted once to the parent and dropped from every row. Returns ``None`` when the
    templates differ, or when none is present, so the caller keeps the per-row copies and
    loses no information (the hoist is lossless: the text lives in exactly one place). The
    single home shared by the skill hoist (:func:`_skill_facts`) and the module-compare
    trait-change hoist.

    The collapse itself is :func:`~arknights_mcp.util.coerce.uniform_str`, shared with the
    importer, which applies the same rule to the four fields the source scopes per skill
    LEVEL -- one home for "the value every entry agrees on, else none".
    """
    return uniform_str(values)


def operator_identity(game_id: str, operator: OperatorRow | None) -> dict[str, object]:
    """``{game_id, display_name?, rarity?, profession?}``: the operator list-row identity.

    Shared by ``find_operators`` and the account roster; a missing build row or value is
    omitted, never null.
    """
    row: dict[str, object] = {"game_id": game_id}
    if operator is not None:
        for key, value in (
            ("display_name", operator.display_name),
            ("rarity", operator.rarity),
            ("profession", operator.profession),
        ):
            if value is not None:
                row[key] = value
    return row


def _summary(
    operator: OperatorRow,
    counts: OperatorSectionCounts,
    factions: tuple[FactionRow, ...],
    collab: bool | None,
) -> OperatorSummary:
    return OperatorSummary(
        rarity=operator.rarity,
        profession=operator.profession,
        subclass_id=operator.subclass_id,
        subclass_name=operator.subclass_name,
        position=operator.position,
        tags=_tags(operator.tag_json),
        obtainable=operator.obtainable,
        phase_count=counts.phases,
        skill_count=counts.skills,
        talent_count=counts.talents,
        module_count=counts.modules,
        factions=factions,
        collab=collab,
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
    """One skill's facts, its effect template hoisted out of the level rows when uniform.

    The in-game effect TEMPLATE is byte-identical across a skill's mastery levels in the
    common case (~30% of a full-operator payload); when every level shares it, it is
    hoisted once to the skill and dropped from each level row. When the levels'
    templates differ it stays per-level so no text is lost (:func:`hoist_uniform_template`).
    """
    level_rows = repo.skill_levels(row.skill_pk)
    hoisted = hoist_uniform_template(lv.gameplay_description for lv in level_rows)
    return OperatorSkillFacts(
        game_id=row.game_id,
        display_name=row.display_name,
        skill_type=row.skill_type,
        sp_type=row.sp_type,
        duration_type=row.duration_type,
        slot_index=row.slot_index,
        unlock_phase=row.unlock_phase,
        unlock_level=row.unlock_level,
        description=hoisted,
        levels=tuple(_skill_level_facts(lv, hoisted=hoisted is not None) for lv in level_rows),
    )


def _skill_level_facts(row: SkillLevelRow, *, hoisted: bool) -> SkillLevelFacts:
    """One skill level; its effect template is dropped when it was hoisted to the skill.

    The four per-level fields need no such flag: the importer already stored
    each one only on the side that owns it -- on the skill when every level agreed, on the
    level when they did not -- so passing the row's value through is byte-lossless.
    """
    return SkillLevelFacts(
        level=row.level,
        sp_cost=row.sp_cost,
        initial_sp=row.initial_sp,
        duration=row.duration,
        range_id=row.range_id,
        blackboard=shape_blackboard(json_load(row.blackboard_json)),
        description=None if hoisted else row.gameplay_description,
        display_name=row.display_name,
        skill_type=row.skill_type,
        sp_type=row.sp_type,
        duration_type=row.duration_type,
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
        blackboard=shape_blackboard(json_load(row.blackboard_json)),
        description=row.gameplay_description,
    )


def cost_item_id(entry: object) -> str | None:
    """The nameable item id of one decoded upgrade-cost entry, else ``None``.

    The single home for the id-*eligibility* predicate: an entry is nameable iff it
    is a ``{id, count, type}`` dict whose ``id`` is a non-empty **string** (game-data cost
    ids are strings). Every consumer -- the id collector (:func:`cost_item_ids`), the name
    pairer (:func:`pair_cost_item_names`), and the un-named detector
    (:func:`~arknights_mcp.mcp.tools._shared.has_unnamed_cost_item`) -- routes through this
    one predicate so all three agree on exactly which entries are candidates for a display
    name; a non-string / empty / missing id is uniformly *not* nameable (never looked up,
    never paired, and so never flagged as un-named).
    """
    if isinstance(entry, dict):
        item_id = entry.get("id")
        if isinstance(item_id, str) and item_id:
            return item_id
    return None


def cost_item_ids(cost: object) -> set[str]:
    """The item game_ids referenced by a decoded upgrade-cost list.

    ``cost`` is the decoded module/skill upgrade cost -- a list of ``{id, count, type}``
    dicts, or ``None``/another shape when the source carried none. Returns the set of
    nameable string ``id`` values (deduped, via :func:`cost_item_id`); a non-list ``cost``
    yields an empty set. The single home for the id-extraction shared by the operator
    + module-compare services.
    """
    if not isinstance(cost, list):
        return set()
    ids: set[str] = set()
    for entry in cost:
        item_id = cost_item_id(entry)
        if item_id is not None:
            ids.add(item_id)
    return ids


def pair_cost_item_names(cost: object, item_names: Mapping[str, str]) -> object:
    """Additively pair each upgrade-cost entry with its item display name.

    For each cost entry whose ``id`` resolves in ``item_names`` (items present in this
    build), a ``display_name`` is added while every original key is preserved -- an
    additive, backward-compatible enrichment. An entry whose id has no imported
    name is left exactly as stored (id + count + type): the id is never given a
    fabricated name; the tool detects the un-named entry and records a
    standing limitation. A non-list ``cost`` (source carried none) is returned
    unchanged. The single home for the pairing shared by the operator +
    module-compare services.
    """
    if not isinstance(cost, list):
        return cost
    paired: list[object] = []
    for entry in cost:
        item_id = cost_item_id(entry)
        if isinstance(entry, dict) and item_id is not None and item_id in item_names:
            # Additive: keep the original keys, append the resolved name. The
            # ``isinstance`` narrows for the spread; ``cost_item_id`` is the shared
            # nameability predicate so this pairs exactly what the detector flags.
            paired.append({**entry, "display_name": item_names[item_id]})
            continue
        paired.append(entry)
    return paired


def _modules_facts(
    repo: OperatorRepository, server: str, operator_pk: int
) -> tuple[OperatorModuleFacts, ...]:
    """Every module's facts, each upgrade-cost item paired with its display name.

    Decodes each module's levels once, collects the upgrade-cost item ids across every
    module for a single region-scoped name lookup, then pairs a ``display_name``
    onto every resolved cost entry (additive). An id with no imported name is left
    as-is -- never fabricated.
    """
    decoded: list[tuple[ModuleRow, list[tuple[ModuleLevelRow, object]]]] = []
    ids: set[str] = set()
    for module in repo.modules(operator_pk):
        levels = [(lv, json_load(lv.cost_json)) for lv in repo.module_levels(module.module_pk)]
        for _lv, cost in levels:
            ids |= cost_item_ids(cost)
        decoded.append((module, levels))
    item_names = repo.item_display_names(server, ids)

    modules: list[OperatorModuleFacts] = []
    for module, levels in decoded:
        # Shape each level's change lists, then collapse the redundant/subset rows
        # and label a -1 summon/token change before emitting them.
        shaped = [
            (
                lv,
                shape_blackboard(json_load(lv.stat_bonus_json)),
                dedup_and_label_changes(shape_blackboard(json_load(lv.trait_changes_json))),
                dedup_and_label_changes(shape_blackboard(json_load(lv.talent_changes_json))),
                pair_cost_item_names(cost, item_names),
            )
            for lv, cost in levels
        ]
        # A change bundle byte-identical across every level is hoisted once to
        # the module (dropped from each level below); ``None`` keeps it per level.
        trait_hoist = hoist_uniform_changes([trait for _lv, _s, trait, _tal, _c in shaped])
        talent_hoist = hoist_uniform_changes([tal for _lv, _s, _t, tal, _c in shaped])
        modules.append(
            OperatorModuleFacts(
                game_id=module.game_id,
                module_type=module.module_type,
                display_name=module.display_name,
                unlock_phase=module.unlock_phase,
                unlock_level=module.unlock_level,
                trait_changes=trait_hoist,
                talent_changes=talent_hoist,
                levels=tuple(
                    ModuleLevelFacts(
                        level=lv.level,
                        stat_bonus=stat,
                        trait_changes=None if trait_hoist is not None else trait,
                        talent_changes=None if talent_hoist is not None else talent,
                        cost=cost,
                    )
                    for lv, stat, trait, talent, cost in shaped
                ),
            )
        )
    return tuple(modules)


def _skin_facts(row: OperatorSkinRow) -> OperatorSkinFacts:
    """Map one ``operator_skins`` row to facts; alt-form = ``tmpl_id != char_id`` (ADR 0015)."""
    return OperatorSkinFacts(
        skin_id=row.skin_id,
        portrait_id=row.portrait_id,
        display_name=row.display_name,
        skin_group_id=row.skin_group_id,
        skin_group_name=row.skin_group_name,
        is_buy_skin=row.is_buy_skin,
        is_alt_form=row.tmpl_id is not None and row.tmpl_id != row.char_id,
    )


def _range_facts(
    conn: sqlite3.Connection,
    server: str,
    phases: tuple[OperatorPhaseFacts, ...],
    skills: tuple[OperatorSkillFacts, ...],
) -> tuple[tuple[RangeGridFacts, ...], tuple[str, ...]]:
    """Resolve the ``range_id`` values the loaded sections emit.

    Collects the ids from the phases and from every skill LEVEL (the source scopes
    ``rangeId`` per level and seven skills genuinely vary across their levels),
    resolves them in one batched region-scoped read, and splits the result into the
    two arms: the grids that resolved, and the ids that did not. An unresolved id is
    never dropped and never guessed -- it is what the limitation names.

    Region-scoped: an ``en`` operator resolves only against ``en`` grids, so en and cn
    never mix. CN's table is a strict superset of EN's at the pinned commit, which
    is exactly why the region must be part of the key rather than a fallback chain.
    """
    wanted = {p.range_id for p in phases if p.range_id} | {
        lv.range_id for s in skills for lv in s.levels if lv.range_id
    }
    if not wanted:
        return (), ()
    resolved = RangeRepository(conn).by_ids(server, wanted)
    facts = tuple(
        RangeGridFacts(
            range_id=row.range_id,
            grids=row.grids,
            rows=range_grid_rows(row.grids),
        )
        for row in (resolved[rid] for rid in sorted(resolved))
    )
    return facts, tuple(sorted(wanted - resolved.keys()))


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
    include_base_skills: bool = False,
    load_skins: bool = False,
) -> OperatorDetailResult:
    """Fetch one operator's facts + opt-in heavy sections for ``server``.

    Read-only; parameterized SQL only. The operator is resolved by its unique
    ``(server, game_id)`` key, so an ``en`` operator is never surfaced under a ``cn``
    query. A missing operator returns ``status == "not_found"`` (the tool maps
    it to the typed envelope). Heavy sections load only when their include flag
    is set, keeping the default response small. Both transports call this
    function.

    ``load_skins`` is wiring-driven, not a client include flag: the
    image-ref-emitting tool sets it iff the emission gate is on, so the named
    gallery is queried only when it will actually be emitted. Empty on a build
    without the skin domain (the repository degrades table-absent to no rows).
    """
    repo = OperatorRepository(conn)
    operator = repo.operator_by_game_id(server, game_id)
    if operator is None:
        return OperatorDetailResult(status="not_found", server=server, operator=None)

    pk = operator.operator_pk
    summary = (
        _summary(
            operator,
            repo.section_counts(pk),
            tuple(repo.factions([pk]).get(pk, [])),
            repo.collab_flags([pk]).get(pk),
        )
        if include_summary
        else None
    )
    base_skills = tuple(repo.base_skills([pk]).get(pk, [])) if include_base_skills else ()
    base_skill_domain_missing = (
        include_base_skills and not base_skills and not repo.has_base_skill_domain()
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
    modules = _modules_facts(repo, operator.server, operator.operator_pk) if include_modules else ()
    skins = (
        tuple(_skin_facts(s) for s in repo.skins(operator.server, operator.operator_pk))
        if load_skins
        else ()
    )
    # Resolve exactly the range ids the LOADED sections emit -- a grid
    # for a section this call did not request would be a fact about nothing.
    # One batched query; the unresolved remainder feeds the limitation arm.
    ranges, unresolved_range_ids = _range_facts(conn, operator.server, phases, skills)
    facts = OperatorFacts(
        server=operator.server,
        game_id=operator.game_id,
        display_name=operator.display_name,
        summary=summary,
        phases=phases,
        skills=skills,
        talents=talents,
        modules=modules,
        skins=skins,
        ranges=ranges,
        base_skills=base_skills,
        base_skill_domain_missing=base_skill_domain_missing,
        unresolved_range_ids=unresolved_range_ids,
        provenance=OperatorProvenance(
            snapshot_id=operator.snapshot_id, imported_at=operator.imported_at
        ),
    )
    return OperatorDetailResult(status="ok", server=operator.server, operator=facts)
