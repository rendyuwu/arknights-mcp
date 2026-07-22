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
  ``(server, game_id)`` key), so en/cn are never silently mixed. The envelope-level
  provenance is unconditional -- ``include_provenance`` only toggles an *extra*
  in-``data`` echo, it can never turn §V5 off.
* **§V22** -- the default response is compact facts + a lightweight summary +
  provenance. The heavy ``phases``/``skills``/``talents``/``modules`` sections are
  opt-in include flags (default off); the envelope's size cap fails closed on any
  oversized payload.
* **§V23** -- every result is a typed-status envelope (``ok``/``not_found``); a
  database failure or any unexpected error fails closed to a fixed, path/trace-free
  envelope via the shared :func:`~arknights_mcp.mcp.tools._shared.run_guarded` guard.
"""

from __future__ import annotations

from arknights_mcp.mcp.envelopes import Provenance, ResponseEnvelope, error, ok
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._shared import (
    BLACKBOARD_KEY_GLOSSARY,
    BLACKBOARD_LIMITATION,
    ConnectionProvider,
    run_guarded,
)
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.operators import GetOperatorInput
from arknights_mcp.services.image_refs import image_ref_to_dict, operator_image_refs
from arknights_mcp.services.operators import (
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
_TOOL_DESCRIPTION = (
    "Fetch one Arknights operator's facts by region + game_id. The default response "
    "is compact identity + a summary (rarity, profession, subclass, position, tags, "
    "and how many phases/skills/talents/modules exist) + provenance; set "
    "include_phases / include_skills / include_talents / include_modules to add each "
    "(bounded) heavy section. When the image-reference source is enabled, an additional "
    "image_refs list of derived portrait/avatar/skin art URLs is included. en/cn are "
    "never mixed. Skill, talent, and module effects include the in-game effect "
    "description template (when present in the source) alongside raw blackboard "
    "key-value data; read the template to interpret the values, and do not infer "
    "mechanics from a key name alone. " + BLACKBOARD_KEY_GLOSSARY
)

_NOT_FOUND_MESSAGE = "no operator matched the given region and game_id"
_NOT_FOUND_ACTION = (
    "verify the server and game_id (use search_entities to find it), or run "
    "`arknights-mcp status` to check the active build"
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
    return {
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
        "range_id": phase.range_id,
    }


def _skill_level_to_dict(level: SkillLevelFacts) -> dict[str, object]:
    return {
        "level": level.level,
        "sp_cost": level.sp_cost,
        "initial_sp": level.initial_sp,
        "duration": level.duration,
        "range_id": level.range_id,
        "blackboard": level.blackboard,
        # §V65 (a)/ADR 0010: the in-game effect TEMPLATE emitted alongside the
        # blackboard so its keys are grounded (additive/optional, §V21).
        "description": level.description,
    }


def _skill_to_dict(skill: OperatorSkillFacts) -> dict[str, object]:
    return {
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


def _module_to_dict(module: OperatorModuleFacts) -> dict[str, object]:
    return {
        "game_id": module.game_id,
        "module_type": module.module_type,
        "display_name": module.display_name,
        "unlock_phase": module.unlock_phase,
        "unlock_level": module.unlock_level,
        "levels": [
            {
                "level": lv.level,
                "stat_bonus": lv.stat_bonus,
                "trait_changes": lv.trait_changes,
                "talent_changes": lv.talent_changes,
                "cost": lv.cost,
            }
            for lv in module.levels
        ],
    }


def _operator_to_dict(
    operator: OperatorFacts, *, include_provenance: bool, image_refs_enabled: bool
) -> dict[str, object]:
    """The typed operator facts + opted-in sections (no prose; §V16/§V18).

    Sections are present only when the service loaded them (their include flag was
    set); ``include_provenance`` toggles an *extra* in-``data`` provenance echo -- the
    envelope always carries the §V5 region provenance regardless. When
    ``image_refs_enabled`` (the combined §T120 config + registry gate), an additive
    ``image_refs`` list of DERIVED portrait/avatar/skin URLs rides along (§V21/§V63);
    when the gate is off the field is absent entirely (backward-compatible default).
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
        # §V63: DERIVED from the operator's already-stored game_id -- no byte, no url
        # stored, no fetch. §V5: rides this operator's OWN region envelope (game_id is
        # region-scoped) so en/cn never mix. §V19: a bounded per-entity attach, never a
        # catalog list/page/search.
        data["image_refs"] = [image_ref_to_dict(r) for r in operator_image_refs(operator.game_id)]
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
    return ok(
        {
            "operator": _operator_to_dict(
                operator,
                include_provenance=include_provenance,
                image_refs_enabled=image_refs_enabled,
            )
        },
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
