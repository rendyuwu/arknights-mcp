"""``get_operator`` MCP tool (§T44; §V5/§V22/§V23; §I.tool).

Bridges the bounded :class:`~arknights_mcp.models.operators.GetOperatorInput` (§T30)
to the shared :func:`~arknights_mcp.services.operators.get_operator` service (§V14)
and wraps the outcome in the typed
:class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope` (§T29). The tool owns no
query logic -- only the model -> service -> envelope mapping -- so both transports
dispatch identical read-only (§V2) behaviour from the single registry.

Three invariants are load-bearing here:

* **§V5** -- ``server`` is required, so every ``ok`` result is region-attributed +
  carries provenance (snapshot_id + imported_at) on the envelope; an ``en`` operator
  is never surfaced under a ``cn`` query (the service resolves by the unique
  ``(server, game_id)`` key), so en/cn are never silently mixed. The envelope is the
  SOLE default provenance carrier (§V66/B64): the envelope-level provenance is
  unconditional -- ``include_provenance`` only toggles an *extra* in-``data`` echo and
  defaults off, so the default response carries the snapshot exactly once and the flag
  can never turn §V5 off.
* **§V22** -- the default response is compact facts + a lightweight summary +
  provenance (once, on the envelope). The heavy
  ``phases``/``skills``/``talents``/``modules`` sections are opt-in include flags
  (default off); the envelope's size cap fails closed on any oversized payload.
* **§V23** -- every result is a typed-status envelope (``ok``/``not_found``); a
  database failure or any unexpected error fails closed to a fixed, path/trace-free
  envelope via the shared :func:`~arknights_mcp.mcp.tools._shared.run_guarded` guard.
"""

from __future__ import annotations

from arknights_mcp.mcp.envelopes import Provenance, ResponseEnvelope, error, ok
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._shared import (
    BLACKBOARD_GLOSSARY_POINTER,
    BLACKBOARD_LIMITATION,
    COST_ITEM_NAME_LIMITATION,
    IMAGE_REFS_PATH_NOTE,
    MODULE_CHANGE_DEDUP_NOTE,
    SKIN_ALT_FORM_NOTE,
    SKIN_GALLERY_PARTIAL_LIMITATION,
    ConnectionProvider,
    attach_image_ref_disclosures,
    has_unnamed_cost_item,
    run_guarded,
)
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.operators import GetOperatorInput
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
    SkillLevelFacts,
    get_operator,
)

_TOOL_NAME = "get_operator"
_TOOL_TITLE = "Get operator"
#: §V71 (f)/§V104 (B145/B142). This description was the longest on the server (~370
#: words) and more than half of it described skin/image-ref MECHANICS -- long enough that
#: a client tool-listing truncated it mid-sentence, cutting the pre-call facts a caller
#: actually needs. Those mechanics moved to where the client reads the values: the
#: ``image_refs_legend`` hoisted onto the response decodes the category/variant labels,
#: and the derived-link + alt-form caveats already ride ``limitations`` on every
#: ref-emitting response. The template-grounding sentence went the same way: it repeated
#: :data:`BLACKBOARD_LIMITATION` almost verbatim, and that limitation rides every response
#: carrying blackboard data. What replaces them is what a caller needs BEFORE the call: an
#: example selector, what each flag adds (``include_provenance`` stated its effect
#: nowhere while envelope provenance shipped unconditionally, so it read as a no-op), and
#: the §V104 value domains for the enums this tool emits.
_TOOL_DESCRIPTION = (
    "Fetch one Arknights operator's facts by region + game_id (for example server en, "
    "game_id char_002_amiya). The default response is compact identity, a summary of the "
    "operator's class and section counts, region, and provenance. Set include_phases / "
    "include_skills / include_talents / include_modules to add each bounded heavy "
    "section. include_provenance only adds a second copy of the snapshot provenance "
    "inside data; the envelope carries it either way. To compare one operator's modules "
    "across their upgrade levels side by side, or for evidence-backed module "
    "observations, use compare_operator_modules instead. en/cn are never mixed. "
    "profession is the class token: PIONEER (Vanguard), WARRIOR (Guard), TANK "
    "(Defender), SPECIAL (Specialist), SUPPORT (Supporter), SNIPER, CASTER, or MEDIC. "
    "position is MELEE or RANGED. A skill's skill_type is AUTO, MANUAL, or PASSIVE. Its "
    "sp_type is INCREASE_WITH_TIME, INCREASE_WHEN_ATTACK, INCREASE_WHEN_TAKEN_DAMAGE, or "
    "a raw source code such as 8. Its duration_type is NONE or AMMO, where NONE means the "
    "source declares no duration type, not that the skill has no duration: read the "
    "level's duration, in seconds. A skill's effect template rides the skill when it is "
    "the same at every level, and the level when the wording differs. With the "
    "image-reference source enabled the response adds derived portrait, avatar, and skin "
    "image_refs, decoded by the image_refs_legend it carries. "
    + IMAGE_REFS_PATH_NOTE
    + " "
    + MODULE_CHANGE_DEDUP_NOTE
    + " "
    + BLACKBOARD_GLOSSARY_POINTER
)

_NOT_FOUND_MESSAGE = "no operator matched the given region and game_id"
_NOT_FOUND_ACTION = (
    "verify the server and game_id (use search_entities to find it), or ask the server "
    "admin to run `arknights-mcp status` to check the active build"
)


def _summary_to_dict(summary: OperatorSummary) -> dict[str, object]:
    """The compact identity + per-section counts (§V22 default; no prose §V16)."""
    return {
        "rarity": summary.rarity,
        "profession": summary.profession,
        "subclass_id": summary.subclass_id,
        "position": summary.position,
        "tags": list(summary.tags),
        "obtainable": summary.obtainable,
        "phase_count": summary.phase_count,
        "skill_count": summary.skill_count,
        "talent_count": summary.talent_count,
        "module_count": summary.module_count,
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
    # §V67: ``range_id`` is an always-optional scalar -- omit the key when the source
    # carried none rather than emit an ambiguous null (additive-safe, §V21).
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
    # §V67: omit the always-optional ``range_id`` scalar when the source carried none.
    if level.range_id is not None:
        out["range_id"] = level.range_id
    # §V66.3: the effect TEMPLATE is emitted once on the parent skill when it is
    # byte-identical across levels; a level carries it only when the templates differ.
    # Omit the key otherwise rather than emit an ambiguous null (§V67).
    if level.description is not None:
        out["description"] = level.description
    return out


def _skill_to_dict(skill: OperatorSkillFacts) -> dict[str, object]:
    out: dict[str, object] = {
        "game_id": skill.game_id,
        "display_name": skill.display_name,
        "skill_type": skill.skill_type,
        "sp_type": skill.sp_type,
        "duration_type": skill.duration_type,
        "slot_index": skill.slot_index,
        "unlock_phase": skill.unlock_phase,
        "unlock_level": skill.unlock_level,
        "levels": [_skill_level_to_dict(lv) for lv in skill.levels],
    }
    # §V66.3/§V65 (a): the in-game effect TEMPLATE, hoisted here once when it is
    # identical across every level (applies to all of them); omitted when it varies by
    # level (each level then carries its own). §V67: absent key, never a null.
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
                # §V65 (a)/ADR 0010: effect TEMPLATE alongside the blackboard (§V21).
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
    # §V66.3/§V83: when a change bundle is byte-identical at every level it is hoisted to the
    # module (below) and omitted here; otherwise it stays per level. Omission = "see the
    # module-level field" (§V67 omit-key discipline).
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
    # §V66.3/§V83: a trait/talent change bundle identical at every level rides the module
    # once here (dropped from each level); absent when it varies (§V67 omit-key, never null).
    if trait_hoisted:
        out["trait_changes"] = module.trait_changes
    if talent_hoisted:
        out["talent_changes"] = module.talent_changes
    return out


def _operator_to_dict(
    operator: OperatorFacts, *, include_provenance: bool, image_refs_enabled: bool
) -> dict[str, object]:
    """The typed operator facts + opted-in sections (no prose; §V16/§V18).

    Sections are present only when the service loaded them (their include flag was
    set); ``include_provenance`` toggles an *extra* in-``data`` provenance echo -- the
    envelope always carries the §V5 region provenance regardless. When
    ``image_refs_enabled`` (the combined §T120 config + registry gate), an additive
    ``image_refs`` list of DERIVED portrait/avatar/skin refs rides along (§V21/§V63) --
    relative paths under the ``data``-level ``image_refs_base_url`` the shaper hoists
    once (§T183/§V66); when the gate is off the field is absent entirely.
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
    if include_provenance:
        data["provenance"] = {
            "snapshot_id": operator.provenance.snapshot_id,
            "imported_at": operator.provenance.imported_at,
        }
    if image_refs_enabled:
        # §V63: DERIVED from the operator's already-stored game_id / imported portrait_id
        # -- no byte, no url stored, no fetch. §V5: rides this operator's OWN region
        # envelope (game_id and skin rows are region-scoped) so en/cn never mix. §V19: a
        # bounded per-entity attach, never a catalog list/page/search.
        # §T182/§V88: with the imported skin domain present the NAMED gallery replaces
        # the derived base-outfit fallback; without it (pre-0014 build / combat-only
        # snapshot) the base `_1b`/`_2b` fallback + the partial-gallery limitation stay
        # (§V21). That choice + the per-row shaping live in the §V37 service home
        # (B125), so this transport only decides WHETHER to attach, never WHAT.
        data["image_refs"] = operator_ref_dicts(operator.game_id, operator.skins)
    return data


def _shape(
    result: OperatorDetailResult, *, include_provenance: bool, image_refs_enabled: bool
) -> ResponseEnvelope:
    """Map the domain result to a typed §V23 envelope (§V5 region + provenance)."""
    if result.status == "not_found" or result.operator is None:
        return error("not_found", _NOT_FOUND_MESSAGE, suggested_action=_NOT_FOUND_ACTION)

    operator = result.operator
    prov = operator.provenance
    # §V65: the skills/talents/modules sections now emit the in-game effect
    # description template alongside the blackboard (path (a)/ADR 0010), but a
    # template may be absent for some effects, so the standing grounding limitation
    # (path (b)) still rides every response that carries one of them (blackboard keys
    # stay raw). A summary-only response emits no blackboard, so it carries no caveat.
    limitations: tuple[str, ...] = ()
    if operator.skills or operator.talents or operator.modules:
        limitations = (BLACKBOARD_LIMITATION,)
    # §V69/§V26 (§T132): a module upgrade-cost item whose display name is absent from the
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
    # §T183/§V66 + §V72/§V26 (§T135, B61): the shared attach (one §V37 home) hoists the
    # mirror base ONCE onto data and appends the derived-unverified limitation, exactly
    # when refs are emitted (get_operator always emits refs when the gate is on).
    limitations = attach_image_ref_disclosures(data, limitations, emits_refs=image_refs_enabled)
    # §V88/§V26 (§T181->§T182, B99): the partial-gallery limitation now rides ONLY the
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
    """Build the ``get_operator`` :class:`ToolSpec` (§T44; §V14).

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build. ``image_refs_enabled`` is the combined §T120 emission gate (config
    private-only posture AND the ``arknights_game_resource`` source enabled, computed
    once at wiring time via :func:`~arknights_mcp.services.image_refs.refs_enabled`); it
    defaults ``False`` so the additive ``image_refs`` field is absent unless the source
    is enabled (§V21/§V63). The returned spec is read-only (§V2) for the single shared
    registry both transports dispatch from (§V14); its ``input_schema`` is the bounded
    model's JSON Schema, so the §V5 required ``server`` + §V18 ``game_id`` cap + the §V22
    include-flag defaults land on the wire exactly as validated.
    """

    def handler(**params: object) -> ResponseEnvelope:
        # §V5/§V18 gate: the bounded model requires a region, caps the game_id
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
                # §T182: wiring-driven, not a client flag -- the named gallery is
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
