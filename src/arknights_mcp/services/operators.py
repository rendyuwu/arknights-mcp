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

import json
import sqlite3
from collections.abc import Iterable, Mapping
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
    """One mastery level of a skill; ``blackboard`` decoded from vetted JSON (§V18).

    ``description`` is the imported in-game effect TEMPLATE (mechanic text referencing
    the blackboard keys; §V65 (a)/ADR 0010), emitted alongside the blackboard for
    grounding. When the template is byte-identical across every one of the skill's
    levels it is hoisted once to the parent :class:`OperatorSkillFacts` and this
    per-level field is ``None`` (§V66.3 payload dedup); it is populated here only when
    the levels' templates differ, so the varying text is never lost.
    """

    level: int
    sp_cost: int | None
    initial_sp: int | None
    duration: float | None
    range_id: str | None
    blackboard: object | None
    description: str | None


@dataclass(frozen=True)
class OperatorSkillFacts:
    """One skill slot: metadata + its ordered mastery levels.

    ``description`` is the in-game effect TEMPLATE hoisted once to the skill when it is
    byte-identical across all the skill's levels (§V66.3); it is ``None`` when the
    template varies by level, in which case each :class:`SkillLevelFacts` carries its
    own. The hoist is byte-lossless -- exactly one of the skill-level or per-level
    ``description`` carries the text.
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
    """One talent variant (potential/phase gated); ``blackboard`` decoded (§V18).

    ``description`` is the imported in-game effect TEMPLATE (mechanic text referencing
    the blackboard keys; §V65 (a)/ADR 0010), emitted alongside the blackboard for
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
    """One module level: the numeric change bundles decoded from vetted JSON (§V18)."""

    level: int
    stat_bonus: object | None
    trait_changes: object | None
    talent_changes: object | None
    cost: object | None


@dataclass(frozen=True)
class OperatorModuleFacts:
    """One module: metadata + its ordered levels.

    ``trait_changes`` / ``talent_changes`` are the per-level change bundle hoisted once to
    the module when it is byte-identical across every level (§V66.3/§V83) -- the client
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


#: Optional keys omitted when their value is ``null`` (§V67 null discipline): a blackboard
#: entry's ``valueStr`` (null for ~60 numeric params on a full operator; §T138/B63) and a
#: trait/talent-change bundle's effect ``description`` template (null when the source carried
#: no template -- e.g. a module's -1 token-effect talent change; §T148). Never emit ``null``
#: for these so a client is not forced to decide "none vs unknown"; omission is additive (§V21).
_NULL_OMIT_KEYS: frozenset[str] = frozenset({"valueStr", "description"})


def shape_blackboard(value: object) -> object:
    """Omit always-null optional keys from a decoded blackboard structure (§V67; §T138/§T148).

    Drops a ``valueStr`` / ``description`` key whose value is ``null`` (:data:`_NULL_OMIT_KEYS`)
    rather than emit ``null`` so the client is not forced to decide "none vs unknown" (§V67).
    Recurses through the decoded dict/list structure so a ``blackboard`` nested inside a
    trait/talent-change bundle is cleaned too, and a bundle's null effect ``description``
    (a module -1 token-effect change carries none) is dropped; a *non-null* value (a real
    string param or an imported template) is kept, and every other key/shape is preserved
    exactly -- omitting an absent-optional key is additive/backward-compatible (§V21). The
    single §V37 home shared by the operator + module-compare read services.
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
    """The single effect TEMPLATE shared by every element of ``values``, else ``None`` (§V66.3).

    Returns the string when ``values`` collapses to exactly one distinct, non-``None``
    template -- the per-level byte-identical case §V66.3 targets, where the template is
    hoisted once to the parent and dropped from every row. Returns ``None`` when the
    templates differ, or when none is present, so the caller keeps the per-row copies and
    loses no information (the hoist is lossless: the text lives in exactly one place). The
    single §V37 home shared by the skill hoist (:func:`_skill_facts`) and the module-compare
    trait-change hoist.
    """
    distinct = set(values)
    if len(distinct) == 1:
        (only,) = distinct
        if isinstance(only, str):
            return only
    return None


#: The keys that IDENTIFY which talent/trait change a bundle is (§V83): two entries sharing
#: these describe the same change (same talent, same potential gate, same unlock condition);
#: every other key (``blackboard``, ``description``) is value-bearing and may be merged.
_EFFECT_IDENTITY_KEYS: frozenset[str] = frozenset(
    {"talentIndex", "requiredPotentialRank", "unlockCondition"}
)


def _canonical(value: object) -> str:
    """A stable, order-independent string for byte-identity comparison of a decoded value."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


def _empty_effect_value(value: object) -> bool:
    """A value-bearing change field that carries nothing (absent, ``[]``, ``{}``, ``""``)."""
    return value is None or value == [] or value == {} or value == ""


def _effect_identity(entry: dict[str, object]) -> str:
    """The identity key of one change bundle -- its (talentIndex, potential, unlock) triple."""
    return _canonical(
        [entry.get(k) for k in ("talentIndex", "requiredPotentialRank", "unlockCondition")]
    )


def _effect_conflict(a: dict[str, object], b: dict[str, object]) -> bool:
    """True when two same-identity bundles carry DIFFERENT non-empty value fields (§V83).

    A conflict means the entries are genuinely different data (e.g. two distinct
    blackboards under the same talent/potential gate), so they must NOT be merged and
    stay as separate rows -- the dedup is byte-lossless (§V66). An empty field never
    conflicts (it is subsumed by the other's value).
    """
    for key in (set(a) | set(b)) - _EFFECT_IDENTITY_KEYS:
        va, vb = a.get(key), b.get(key)
        if not _empty_effect_value(va) and not _empty_effect_value(vb) and va != vb:
            return True
    return False


def _merge_effect(target: dict[str, object], entry: dict[str, object]) -> None:
    """Fold ``entry``'s non-empty value fields into ``target`` in place (§V83).

    Only fills a field ``target`` lacks or left empty -- a conflicting field is never
    reached (the caller checks :func:`_effect_conflict` first). The merged row is a
    superset of both inputs, so no information is lost (§V66 byte-lossless).
    """
    for key, value in entry.items():
        if key in _EFFECT_IDENTITY_KEYS:
            continue
        if _empty_effect_value(target.get(key)) and not _empty_effect_value(value):
            target[key] = value


def dedup_effect_changes(changes: object) -> object:
    """Collapse duplicate/subset talent/trait change bundles into one row each (§V83/§V66).

    Two bundles sharing an identity -- (``talentIndex``, ``requiredPotentialRank``,
    ``unlockCondition``) -- describe the SAME change; the source sometimes emits it several
    times split across parts (a prose-only copy, a blackboard-only copy, a blackboard+prose
    copy). They are merged into one row carrying the union of their non-empty fields, so a
    single talent no longer emits N near-identical rows (B88). The merge is byte-lossless:
    only non-conflicting entries collapse (:func:`_effect_conflict`); a genuine conflict
    (two distinct non-empty blackboards under one gate) keeps the rows separate. Group
    order follows first appearance; a non-list value is returned unchanged. The single §V37
    home shared by the operator + module-compare read services.
    """
    if not isinstance(changes, list):
        return changes
    groups: list[object] = []
    identities: list[str | None] = []
    for entry in changes:
        if not isinstance(entry, dict):
            groups.append(entry)
            identities.append(None)
            continue
        identity = _effect_identity(entry)
        for i, existing in enumerate(groups):
            if (
                identities[i] == identity
                and isinstance(existing, dict)
                and not _effect_conflict(existing, entry)
            ):
                _merge_effect(existing, entry)
                break
        else:
            groups.append(dict(entry))
            identities.append(identity)
    return groups


def label_token_effects(changes: object) -> object:
    """Label a summon/token talent change (``talentIndex == -1``) with ``applies_to`` (§V83).

    A ``talentIndex`` of ``-1`` is a game-data sentinel for an effect that applies to the
    operator's summon/token rather than the operator itself; emitted bare it forces a client
    to guess (B88), so an ``applies_to: "token"`` label is added (additive, §V21). Every
    other bundle is passed through unchanged; a non-list value is returned as-is. The single
    §V37 home shared by both read services (a trait change carries no ``talentIndex`` so it
    is untouched).
    """
    if not isinstance(changes, list):
        return changes
    labelled: list[object] = []
    for entry in changes:
        if isinstance(entry, dict) and entry.get("talentIndex") == -1 and "applies_to" not in entry:
            labelled.append({**entry, "applies_to": "token"})
        else:
            labelled.append(entry)
    return labelled


def dedup_and_label_changes(changes: object) -> object:
    """Dedup subset/duplicate change rows then label token effects (§V83; §V37 home).

    The emit-shaping pair applied to every per-level talent/trait change list by both read
    services: :func:`dedup_effect_changes` collapses the redundant rows, then
    :func:`label_token_effects` tags a ``-1`` summon/token change. Operates on an
    already-``shape_blackboard``ed (and, for a hoisted trait template, description-stripped)
    list so the two pipelines stay identical after their differing pre-steps.
    """
    return label_token_effects(dedup_effect_changes(changes))


def hoist_uniform_changes(per_level: list[object | None]) -> object | None:
    """The change bundle every present level shares byte-identically, else ``None`` (§V66.3/§V83).

    Returns the bundle when there are at least two present levels and each carries a
    non-empty, byte-identical change list -- the per-level repeat §V66.3 targets, where the
    bundle is hoisted once to the module and dropped from every level. Returns ``None`` when
    a level carries none or a differing bundle, so the caller keeps the per-level copies and
    loses nothing (byte-lossless). The single §V37 home shared by both read services; sibling
    to :func:`hoist_uniform_template` (which hoists a single description string).
    """
    if len(per_level) < 2 or any(_empty_effect_value(v) for v in per_level):
        return None
    if len({_canonical(v) for v in per_level}) == 1:
        return per_level[0]
    return None


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
    """One skill's facts, its effect template hoisted out of the level rows when uniform (§V66.3).

    The in-game effect TEMPLATE is byte-identical across a skill's mastery levels in the
    common case (~30% of a full-operator payload); when every level shares it, it is
    hoisted once to the skill and dropped from each level row (§V66.3). When the levels'
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
    """One skill level; its effect template is dropped when it was hoisted to the skill (§V66.3)."""
    return SkillLevelFacts(
        level=row.level,
        sp_cost=row.sp_cost,
        initial_sp=row.initial_sp,
        duration=row.duration,
        range_id=row.range_id,
        blackboard=shape_blackboard(json_load(row.blackboard_json)),
        description=None if hoisted else row.gameplay_description,
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
    """The nameable item id of one decoded upgrade-cost entry, else ``None`` (§T132/§V69).

    The single §V37 home for the id-*eligibility* predicate: an entry is nameable iff it
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
    """The item game_ids referenced by a decoded upgrade-cost list (§T132/§V69).

    ``cost`` is the decoded module/skill upgrade cost -- a list of ``{id, count, type}``
    dicts, or ``None``/another shape when the source carried none. Returns the set of
    nameable string ``id`` values (deduped, via :func:`cost_item_id`); a non-list ``cost``
    yields an empty set. The single §V37 home for the id-extraction shared by the operator
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
    """Additively pair each upgrade-cost entry with its item display name (§T132/§V69).

    For each cost entry whose ``id`` resolves in ``item_names`` (items present in this
    build), a ``display_name`` is added while every original key is preserved -- an
    additive, backward-compatible enrichment (§V21). An entry whose id has no imported
    name is left exactly as stored (id + count + type): the id is never given a
    fabricated name (§V26/§V69); the tool detects the un-named entry and records a
    standing limitation. A non-list ``cost`` (source carried none) is returned
    unchanged. The single §V37 home for the pairing shared by the operator +
    module-compare services.
    """
    if not isinstance(cost, list):
        return cost
    paired: list[object] = []
    for entry in cost:
        item_id = cost_item_id(entry)
        if isinstance(entry, dict) and item_id is not None and item_id in item_names:
            # Additive (§V21): keep the original keys, append the resolved name. The
            # ``isinstance`` narrows for the spread; ``cost_item_id`` is the shared
            # nameability predicate (§V37) so this pairs exactly what the detector flags.
            paired.append({**entry, "display_name": item_names[item_id]})
            continue
        paired.append(entry)
    return paired


def _modules_facts(
    repo: OperatorRepository, server: str, operator_pk: int
) -> tuple[OperatorModuleFacts, ...]:
    """Every module's facts, each upgrade-cost item paired with its display name (§T132/§V69).

    Decodes each module's levels once, collects the upgrade-cost item ids across every
    module for a single region-scoped name lookup (§V5), then pairs a ``display_name``
    onto every resolved cost entry (additive, §V21). An id with no imported name is left
    as-is -- never fabricated (§V26/§V69).
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
        # §V83/§V67: shape each level's change lists, then collapse the redundant/subset rows
        # and label a -1 summon/token change (B88) before emitting them.
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
        # §V66.3/§V83: a change bundle byte-identical across every level is hoisted once to
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
    modules = _modules_facts(repo, operator.server, operator.operator_pk) if include_modules else ()
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
