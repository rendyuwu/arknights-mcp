"""``get_operator`` MCP tool.

Bridges the bounded :class:`~arknights_mcp.models.operators.GetOperatorInput` to the
shared :func:`~arknights_mcp.services.operators.get_operator` service and wraps the
outcome in the typed :class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope`. The tool
owns no query logic -- only the model -> service -> envelope mapping -- so both
transports dispatch identical read-only behaviour from the single registry.

Three rules are load-bearing here:

* ``server`` is required, so every ``ok`` result is region-attributed + carries
  provenance (snapshot_id + imported_at) on the envelope; an ``en`` operator
  is never surfaced under a ``cn`` query (the service resolves by the unique
  ``(server, game_id)`` key), so en/cn are never silently mixed. The envelope is the
  SOLE default provenance carrier: the envelope-level provenance is
  unconditional -- ``include_provenance`` only toggles an *extra* in-``data`` echo and
  defaults off, so the default response carries the snapshot exactly once and the flag
  can never turn that off.
* The default response is compact facts + a lightweight summary +
  provenance (once, on the envelope). The heavy
  ``phases``/``skills``/``talents``/``modules`` sections are opt-in include flags
  (default off); the envelope's size cap fails closed on any oversized payload.
* Every result is a typed-status envelope (``ok``/``not_found``); a
  database failure or any unexpected error fails closed to a fixed, path/trace-free
  envelope via the shared :func:`~arknights_mcp.mcp.tools._shared.run_guarded` guard.
"""

from __future__ import annotations

from collections.abc import Iterable

from arknights_mcp.mcp.envelopes import Provenance, ResponseEnvelope, error, ok
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._enum_legend import attach_enum_legend
from arknights_mcp.mcp.tools._shared import (
    BLACKBOARD_GLOSSARY_POINTER,
    BLACKBOARD_LIMITATION,
    COST_ITEM_NAME_LIMITATION,
    IMAGE_REFS_PATH_NOTE,
    MODULE_CHANGE_DEDUP_NOTE,
    MODULE_TYPE_NOTE,
    SKIN_ALT_FORM_NOTE,
    SKIN_GALLERY_PARTIAL_LIMITATION,
    SUBCLASS_NAME_LIMITATION,
    ConnectionProvider,
    attach_image_ref_disclosures,
    has_unnamed_cost_item,
    run_guarded,
)
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.operators import GetOperatorInput
from arknights_mcp.services.base_skills import (
    BASE_SKILL_DOMAIN_MISSING_LIMITATION,
    BASE_SKILL_SLOT_NOTE,
    COLLAB_LIMITATION,
    base_skill_entries,
    faction_entries,
)
from arknights_mcp.services.image_refs import operator_ref_dicts
from arknights_mcp.services.operators import (
    ModuleLevelFacts,
    OperatorDetailResult,
    OperatorFacts,
    OperatorModuleFacts,
    OperatorPhaseFacts,
    OperatorSkillFacts,
    OperatorSummary,
    OperatorTalentFacts,
    RangeGridFacts,
    SkillLevelFacts,
    get_operator,
)
from arknights_mcp.services.range_grid import RANGE_GRID_SYMBOLS, unresolved_range_limitation

_TOOL_NAME = "get_operator"
_TOOL_TITLE = "Get operator"
#: This description was the longest on the server (~370 words) and more than half of it
#: described skin/image-ref MECHANICS -- long enough that a client tool-listing truncated
#: it mid-sentence, cutting the pre-call facts a caller actually needs. Those mechanics
#: moved to where the client reads the values: the ``image_refs_legend`` hoisted onto the
#: response decodes the category/variant labels, and the derived-link + alt-form caveats
#: already ride ``limitations`` on every ref-emitting response. The template-grounding
#: sentence went the same way: it repeated :data:`BLACKBOARD_LIMITATION` almost verbatim,
#: and that limitation rides every response carrying blackboard data. What replaces them is
#: what a caller needs BEFORE the call: an example selector, what each flag adds
#: (``include_provenance`` stated its effect nowhere while envelope provenance shipped
#: unconditionally, so it read as a no-op), and the value domains for the enums this tool
#: emits.
#:
#: A later pass finished the job that contention had blocked: it could only reach the
#: budget because the domains it was told to ADD (+570) cancelled most of what it cut. The
#: response-side legend is now an EQUAL home for an OUTPUT domain, so those five domains
#: moved to ``enum_legend`` where they arrive beside the values, and
#: :data:`MODULE_CHANGE_DEDUP_NOTE` moved to a standing limitation -- it describes what the
#: emitted module payload OMITTED, which is read post-call. Nothing was deleted to hit the
#: number.
_TOOL_DESCRIPTION = (
    "Fetch one Arknights operator's facts by region + game_id (for example server en, "
    "game_id char_002_amiya). The default response is compact identity, a summary of the "
    "operator's class and section counts, region, and provenance. Set include_phases / "
    "include_skills / include_talents / include_modules / include_base_skills to add each "
    "bounded heavy section. include_provenance only adds a second copy of the snapshot "
    "provenance inside data; the envelope carries it either way. To compare one operator's "
    "modules across their upgrade levels side by side, or for evidence-backed module "
    "observations, use compare_operator_modules instead. en/cn are never mixed. "
    "base_skills entries carry slot, room_type, description, unlock_elite and unlock_level; "
    "a later entry in a slot replaces the earlier one once unlocked. The summary adds "
    "factions and collab. "
    "The operator's rarity is their star count as an integer, 1 to 6. "
    "The response's enum_legend gives the values of profession and position, and of a "
    "skill's skill_type, sp_type, and duration_type. A skill's effect template rides the "
    "skill when it is the same at every level, and the level when the wording differs. "
    "With the image-reference source enabled the response adds derived portrait, avatar, "
    "and skin image_refs, decoded by the image_refs_legend it carries. "
    + IMAGE_REFS_PATH_NOTE
    + " "
    + BLACKBOARD_GLOSSARY_POINTER
)

_NOT_FOUND_MESSAGE = "no operator matched the given region and game_id"
_NOT_FOUND_ACTION = (
    "verify the server and game_id (use search_entities to find it), or ask the server "
    "admin to run `arknights-mcp status` to check the active build"
)

#: Which of this tool's six enum domains ride which response. The identity
#: pair comes with the always-present summary; the three skill domains only when
#: ``include_skills`` actually put skills on the wire, and ``applies_to`` only when
#: ``include_modules`` put change bundles there (it is the label those bundles
#: carry). Their union is the tool's entry in the shared :data:`TOOL_ENUM_LEGEND_FIELDS`
#: table (asserted in the enum-legend guard), so a field added there can never be silently
#: left unattached here.
_SUMMARY_ENUM_FIELDS = ("profession", "position")
_SKILL_ENUM_FIELDS = ("skill_type", "sp_type", "duration_type")
_MODULE_ENUM_FIELDS = ("applies_to",)
_BASE_SKILL_ENUM_FIELDS = ("room_type",)

#: The four ``skill_table`` fields the source scopes PER LEVEL. One home for
#: the list the variance detector and both emit sites below walk. Local to this module,
#: not :mod:`_shared`: ``get_operator`` is the only tool that puts skills on the wire, and
#: a shared home for a single caller is indirection, not dedup.
LEVEL_SCOPED_SKILL_FIELDS: tuple[str, ...] = (
    "display_name",
    "skill_type",
    "sp_type",
    "duration_type",
)

#: The routing note for a skill whose name or enum values are
#: not the same at every mastery level. The source scopes those four fields PER LEVEL, and
#: the importer used to store level 1's value as the skill's -- so ``sktok_mjcsdw`` claimed
#: the unnamed ``sp_type`` code its own level 2 names. They are now stored where the source
#: scopes them: the skill carries a value only when every level agrees, and otherwise the
#: key is absent there (omit, never a null and never a representative pick) and each
#: level carries its own. Absence alone would read as "the source has none" (the
#: availability case), so a response carrying a varying skill also carries this note,
#: which says where the values went (a bounded view routes to the fuller one). Attached
#: only when a skill in THIS response actually varies. Client-facing text, so no internal
#: cites/jargon; short sentences.
SKILL_LEVEL_VARIANCE_NOTE = (
    "One or more skills here change name, skill_type, sp_type, or duration_type between "
    "mastery levels. Those fields are omitted on the skill and given on each level "
    "instead, so read them from the skill's levels list. A field present on the skill "
    "applies to all of its levels."
)


def _has_level_varying_skill(skills: Iterable[OperatorSkillFacts]) -> bool:
    """True when any emitted skill carries one of the four fields per LEVEL.

    The detector for :data:`SKILL_LEVEL_VARIANCE_NOTE`. A skill varies exactly when the
    parent value is absent while a level supplies one -- the shape the importer writes when
    the source's levels disagree. A field absent on BOTH sides is absent from the source,
    a different case this note must not claim.
    """
    return any(
        getattr(skill, field) is None and any(getattr(lv, field) is not None for lv in skill.levels)
        for skill in skills
        for field in LEVEL_SCOPED_SKILL_FIELDS
    )


def _summary_to_dict(summary: OperatorSummary) -> dict[str, object]:
    """The compact identity + per-section counts (compact default; no prose)."""
    return {
        "rarity": summary.rarity,
        "profession": summary.profession,
        "subclass_id": summary.subclass_id,
        # The opaque id ships paired with its name. Omitted, never null, when the build has
        # no name for it -- the limitation is then the sole signal.
        **({"subclass_name": summary.subclass_name} if summary.subclass_name else {}),
        "position": summary.position,
        "tags": list(summary.tags),
        "obtainable": summary.obtainable,
        "phase_count": summary.phase_count,
        "skill_count": summary.skill_count,
        "talent_count": summary.talent_count,
        "module_count": summary.module_count,
        **({"factions": faction_entries(summary.factions)} if summary.factions else {}),
        **({"collab": summary.collab} if summary.collab is not None else {}),
    }


def _phase_to_dict(phase: OperatorPhaseFacts) -> dict[str, object]:
    out: dict[str, object] = {
        "phase": phase.phase,
        "max_level": phase.max_level,
        "max_hp": phase.max_hp,
        "atk": phase.atk,
        "def": phase.def_,
        "res": phase.res,
        "redeploy_time": phase.redeploy_time,
        "cost": phase.cost,
        "block_count": phase.block_count,
        "attack_interval": phase.attack_interval,
    }
    # ``range_id`` is optional -- omit the key when the source carried none rather
    # than emit an ambiguous null (additive-safe). When present it is resolvable
    # through the response-level ``ranges`` map, or named by the unresolved limitation;
    # it is never a bare id with neither.
    if phase.range_id is not None:
        out["range_id"] = phase.range_id
    return out


def _skill_level_to_dict(level: SkillLevelFacts) -> dict[str, object]:
    out: dict[str, object] = {
        "level": level.level,
        "sp_cost": level.sp_cost,
        "initial_sp": level.initial_sp,
        "duration": level.duration,
        "blackboard": level.blackboard,
    }
    # Omit the optional ``range_id`` scalar when the source carried none. The
    # source scopes it per LEVEL and seven skills really do vary across their levels,
    # so it stays here rather than hoisting to the skill; the response-level
    # ``ranges`` map resolves whichever ids the levels name.
    if level.range_id is not None:
        out["range_id"] = level.range_id
    # The effect TEMPLATE is emitted once on the parent skill when it is
    # byte-identical across levels; a level carries it only when the templates differ.
    # Omit the key otherwise rather than emit an ambiguous null.
    if level.description is not None:
        out["description"] = level.description
    # The source scopes name + the three enums per LEVEL. They ride
    # the skill when every level agrees and this level then omits them; a level carries
    # its own only when the levels disagree, so the discarded values are back on the wire.
    out.update(_level_scoped(level))
    return out


def _level_scoped(facts: OperatorSkillFacts | SkillLevelFacts) -> dict[str, object]:
    """The four per-level fields ``facts`` actually carries (omit-key).

    Emitted on whichever side owns the value: the skill when every level agrees, the level
    when they disagree. A ``None`` is never written out -- on the skill it would claim the
    source has no value when the levels merely differ, and on a level it would repeat what
    the skill already states (absent key, never an ambiguous null).
    """
    return {
        field: value
        for field in LEVEL_SCOPED_SKILL_FIELDS
        if (value := getattr(facts, field)) is not None
    }


def _skill_to_dict(skill: OperatorSkillFacts) -> dict[str, object]:
    out: dict[str, object] = {
        "game_id": skill.game_id,
        # Each of the four rides the skill only when it is the value
        # every level shares. Absent means either the levels disagree -- each level then
        # carries its own and SKILL_LEVEL_VARIANCE_NOTE says where to read them -- or the
        # source carried none; never a null, and never level 1's value passed off as the
        # skill's.
        **_level_scoped(skill),
        "slot_index": skill.slot_index,
        "unlock_phase": skill.unlock_phase,
        "unlock_level": skill.unlock_level,
        "levels": [_skill_level_to_dict(lv) for lv in skill.levels],
    }
    # The in-game effect TEMPLATE, hoisted here once when it is
    # identical across every level (applies to all of them); omitted when it varies by
    # level (each level then carries its own). Absent key, never a null.
    if skill.description is not None:
        out["description"] = skill.description
    return out


def _talent_to_dict(talent: OperatorTalentFacts) -> dict[str, object]:
    return {
        "talent_index": talent.talent_index,
        "display_name": talent.display_name,
        "variants": [
            {
                "variant_index": v.variant_index,
                "unlock_phase": v.unlock_phase,
                "unlock_level": v.unlock_level,
                "potential_rank": v.potential_rank,
                "blackboard": v.blackboard,
                # Effect TEMPLATE alongside the blackboard (additive, ADR 0010).
                "description": v.description,
            }
            for v in talent.variants
        ],
    }


def _module_level_to_dict(
    lv: ModuleLevelFacts, *, trait_hoisted: bool, talent_hoisted: bool
) -> dict[str, object]:
    out: dict[str, object] = {
        "level": lv.level,
        "stat_bonus": lv.stat_bonus,
        "cost": lv.cost,
    }
    # When a change bundle is byte-identical at every level it is hoisted to the
    # module (below) and omitted here; otherwise it stays per level. Omission = "see the
    # module-level field" (omit-key discipline).
    if not trait_hoisted:
        out["trait_changes"] = lv.trait_changes
    if not talent_hoisted:
        out["talent_changes"] = lv.talent_changes
    return out


def _module_to_dict(module: OperatorModuleFacts) -> dict[str, object]:
    trait_hoisted = module.trait_changes is not None
    talent_hoisted = module.talent_changes is not None
    out: dict[str, object] = {
        "game_id": module.game_id,
        "module_type": module.module_type,
        "display_name": module.display_name,
        "unlock_phase": module.unlock_phase,
        "unlock_level": module.unlock_level,
        "levels": [
            _module_level_to_dict(lv, trait_hoisted=trait_hoisted, talent_hoisted=talent_hoisted)
            for lv in module.levels
        ],
    }
    # A trait/talent change bundle identical at every level rides the module
    # once here (dropped from each level); absent when it varies (omit-key, never null).
    if trait_hoisted:
        out["trait_changes"] = module.trait_changes
    if talent_hoisted:
        out["talent_changes"] = module.talent_changes
    return out


def _ranges_to_dict(ranges: tuple[RangeGridFacts, ...]) -> dict[str, object]:
    """The response-level ``range_id`` -> grid resolution map.

    Hoisted once per response rather than inlined at each site: three phases and ~21
    skill levels reference a mean of two distinct grids, so pairing at every emission
    would repeat the same coordinates two dozen times (dedup). The symbol alphabet
    rides the container once for the same reason.

    ``symbols`` decodes ``rows``. It is the SERVER's own alphabet, not a source value
    domain, so it belongs with the payload rather than in the enum legend -- the
    same reasoning that keeps ``tile_grid.absent_symbol`` beside its grid.
    """
    return {
        "symbols": dict(RANGE_GRID_SYMBOLS),
        "entries": {
            r.range_id: {
                # The imported fact: deploy-tile-relative offsets, machine-usable
                # without parsing the board (no forced second step).
                "grids": [{"row": row, "col": col} for row, col in r.grids],
                # The board is omitted, never emitted empty, when the frame
                # ceiling refused it -- an empty rows list would read as "no board".
                **({"rows": list(r.rows)} if r.rows else {}),
                "cell_count": len(r.grids),
            }
            for r in ranges
        },
    }


def _operator_to_dict(
    operator: OperatorFacts, *, include_provenance: bool, image_refs_enabled: bool
) -> dict[str, object]:
    """The typed operator facts + opted-in sections (no prose).

    Sections are present only when the service loaded them (their include flag was
    set); ``include_provenance`` toggles an *extra* in-``data`` provenance echo -- the
    envelope always carries the region provenance regardless. When
    ``image_refs_enabled`` (the combined config + registry gate), an additive
    ``image_refs`` list of DERIVED portrait/avatar/skin refs rides along --
    relative paths under the ``data``-level ``image_refs_base_url`` the shaper hoists
    once; when the gate is off the field is absent entirely.
    """
    data: dict[str, object] = {
        "server": operator.server,
        "game_id": operator.game_id,
        "display_name": operator.display_name,
    }
    if operator.summary is not None:
        data["summary"] = _summary_to_dict(operator.summary)
    if operator.phases:
        data["phases"] = [_phase_to_dict(p) for p in operator.phases]
    if operator.skills:
        data["skills"] = [_skill_to_dict(s) for s in operator.skills]
    if operator.talents:
        data["talents"] = [_talent_to_dict(t) for t in operator.talents]
    if operator.modules:
        data["modules"] = [_module_to_dict(m) for m in operator.modules]
    if operator.base_skills:
        data["base_skills"] = base_skill_entries(operator.base_skills)
    # The grids resolving the range_ids the emitted sections carry. Absent when nothing
    # emitted a range_id, or when none of them resolved -- the limitation is then the sole
    # signal (no empty map claiming "no grids exist").
    if operator.ranges:
        data["ranges"] = _ranges_to_dict(operator.ranges)
    if include_provenance:
        data["provenance"] = {
            "snapshot_id": operator.provenance.snapshot_id,
            "imported_at": operator.provenance.imported_at,
        }
    if image_refs_enabled:
        # DERIVED from the operator's already-stored game_id / imported portrait_id
        # -- no byte, no url stored, no fetch. Rides this operator's OWN region
        # envelope (game_id and skin rows are region-scoped) so en/cn never mix. A
        # bounded per-entity attach, never a catalog list/page/search.
        # With the imported skin domain present the NAMED gallery replaces
        # the derived base-outfit fallback; without it (pre-0014 build / combat-only
        # snapshot) the base `_1b`/`_2b` fallback + the partial-gallery limitation stay
        # (additive). That choice + the per-row shaping live in the shared service home,
        # so this transport only decides WHETHER to attach, never WHAT.
        data["image_refs"] = operator_ref_dicts(operator.game_id, operator.skins)
    return data


def _shape(
    result: OperatorDetailResult, *, include_provenance: bool, image_refs_enabled: bool
) -> ResponseEnvelope:
    """Map the domain result to a typed envelope (region + provenance)."""
    if result.status == "not_found" or result.operator is None:
        return error("not_found", _NOT_FOUND_MESSAGE, suggested_action=_NOT_FOUND_ACTION)

    operator = result.operator
    prov = operator.provenance
    # The skills/talents/modules sections emit the in-game effect
    # description template alongside the blackboard (path (a)/ADR 0010), but a
    # template may be absent for some effects, so the standing grounding limitation
    # (path (b)) still rides every response that carries one of them (blackboard keys
    # stay raw). A summary-only response emits no blackboard, so it carries no caveat.
    limitations: tuple[str, ...] = ()
    if operator.skills or operator.talents or operator.modules:
        limitations = (BLACKBOARD_LIMITATION,)
    # A skill whose name or enum values differ between mastery
    # levels emits them per level, with the skill's own key absent -- absence alone reads
    # as "the source has none", so the note says where the values are instead.
    if _has_level_varying_skill(operator.skills):
        limitations = (*limitations, SKILL_LEVEL_VARIANCE_NOTE)
    # A module upgrade-cost item whose display name is absent from the
    # build is emitted as a bare id, so add the standing cost-name limitation instead of
    # leaving a bare id (never fabricate a name). Additive to the blackboard caveat.
    if has_unnamed_cost_item(lv.cost for m in operator.modules for lv in m.levels):
        limitations = (*limitations, COST_ITEM_NAME_LIMITATION)
    data: dict[str, object] = {
        "operator": _operator_to_dict(
            operator,
            include_provenance=include_provenance,
            image_refs_enabled=image_refs_enabled,
        )
    }
    # The shared attach (one home) hoists the
    # mirror base ONCE onto data and appends the derived-unverified limitation, exactly
    # when refs are emitted (get_operator always emits refs when the gate is on).
    limitations = attach_image_ref_disclosures(data, limitations, emits_refs=image_refs_enabled)
    # The partial-gallery limitation rides ONLY the
    # fallback path -- no imported skin rows means the emitted skin refs are the derived
    # base-outfit art alone, and that deferral stays visible. On the named-gallery path
    # the outfit list is complete; what remains partial is the alt-form axis, disclosed
    # (only when an alt-form ref is actually emitted) by the standing alt-form note
    # (ADR 0015: labeled, never silently folded into the base operator).
    if image_refs_enabled:
        if not operator.skins:
            limitations = (*limitations, SKIN_GALLERY_PARTIAL_LIMITATION)
        elif any(s.is_alt_form for s in operator.skins):
            limitations = (*limitations, SKIN_ALT_FORM_NOTE)
    # The module dedup/labelling note describes what the emitted module
    # payload OMITTED, so it rides the response that carries modules rather than the
    # description of both module-emitting tools.
    if operator.modules:
        limitations = (*limitations, MODULE_CHANGE_DEDUP_NOTE, MODULE_TYPE_NOTE)
    # The subclass id ships with its name; when this build has no name for it,
    # the id ships alone and the limitation is the sole signal (no null, no guess).
    if (
        operator.summary is not None
        and operator.summary.subclass_id
        and not operator.summary.subclass_name
    ):
        limitations = (*limitations, SUBCLASS_NAME_LIMITATION)
    # The other arm: an emitted range_id this build has no
    # grid for (snapshot without range_table, or a DB predating migration 0020) ships as
    # a bare id plus this limitation naming it -- never a fabricated grid, and never the
    # silence that made "what is this skill's range" unanswerable.
    if (range_note := unresolved_range_limitation(operator.unresolved_range_ids)) is not None:
        limitations = (*limitations, range_note)
    if operator.base_skills:
        limitations = (*limitations, BASE_SKILL_SLOT_NOTE)
    if operator.base_skill_domain_missing:
        limitations = (*limitations, BASE_SKILL_DOMAIN_MISSING_LIMITATION)
    if operator.summary is not None and operator.summary.collab is True:
        limitations = (*limitations, COLLAB_LIMITATION)
    # Each domain rides the response that actually emits its field -- a legend
    # for a section this call did not request would be noise. Both subsets are
    # drawn from the one shared table, so the tool and the enum-legend guard cannot drift.
    limitations = attach_enum_legend(
        data,
        (
            *(_SUMMARY_ENUM_FIELDS if operator.summary is not None else ()),
            *(_SKILL_ENUM_FIELDS if operator.skills else ()),
            *(_MODULE_ENUM_FIELDS if operator.modules else ()),
            *(_BASE_SKILL_ENUM_FIELDS if operator.base_skills else ()),
        ),
        limitations,
    )
    return ok(
        data,
        provenance=[
            Provenance(
                server=operator.server,
                snapshot_id=prov.snapshot_id,
                imported_at=prov.imported_at,
            )
        ],
        limitations=limitations,
    )


def build_get_operator_spec(
    get_conn: ConnectionProvider, *, image_refs_enabled: bool = False
) -> ToolSpec:
    """Build the ``get_operator`` :class:`ToolSpec`.

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build. ``image_refs_enabled`` is the combined emission gate (config
    private-only posture AND the ``arknights_game_resource`` source enabled, computed
    once at wiring time via :func:`~arknights_mcp.services.image_refs.refs_enabled`); it
    defaults ``False`` so the additive ``image_refs`` field is absent unless the source
    is enabled. The returned spec is read-only for the single shared
    registry both transports dispatch from; its ``input_schema`` is the bounded
    model's JSON Schema, so the required ``server`` + ``game_id`` cap + the
    include-flag defaults land on the wire exactly as validated.
    """

    def handler(**params: object) -> ResponseEnvelope:
        # The bounded model requires a region, caps the game_id
        # length, and rejects an unknown parameter *before* any query runs -- a
        # ValidationError propagates as a protocol-level rejection.
        parsed = GetOperatorInput.model_validate(params)
        return run_guarded(
            get_conn,
            lambda conn: get_operator(
                conn,
                server=parsed.server,
                game_id=parsed.game_id,
                include_summary=parsed.include_summary,
                include_phases=parsed.include_phases,
                include_skills=parsed.include_skills,
                include_talents=parsed.include_talents,
                include_modules=parsed.include_modules,
                include_base_skills=parsed.include_base_skills,
                # Wiring-driven, not a client flag -- the named gallery is
                # queried only when the emission gate will actually emit it.
                load_skins=image_refs_enabled,
            ),
            lambda result: _shape(
                result,
                include_provenance=parsed.include_provenance,
                image_refs_enabled=image_refs_enabled,
            ),
        )

    return ToolSpec(
        name=_TOOL_NAME,
        title=_TOOL_TITLE,
        description=_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(GetOperatorInput),
    )
