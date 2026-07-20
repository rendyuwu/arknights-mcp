"""``compare_operator_modules`` MCP tool (§T45; §V5/§V7/§V23; §I.tool).

Bridges the bounded
:class:`~arknights_mcp.models.operators.CompareOperatorModulesInput` (§T30) to the
shared :func:`~arknights_mcp.services.module_compare.compare_operator_modules`
service (§V14) and wraps the outcome in the typed
:class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope` (§T29). The tool owns no
query logic -- only the model -> service -> envelope mapping -- so both transports
dispatch identical read-only (§V2) behaviour from the single registry.

Two invariants are load-bearing here:

* **§V5** -- ``server`` is required, so every ``ok`` result is region-attributed +
  carries provenance on the envelope; an ``en`` operator is never surfaced under a
  ``cn`` query (the service resolves by the unique ``(server, game_id)`` key).
* **§V7** -- ``with_observations`` mode surfaces the module analyzer's evidence-
  backed observations (§V6) -- capability facts about what each module changes,
  never a "mandatory"/"best-in-slot" recommendation. ``facts_only`` returns the
  typed comparison alone.

Every result is a typed-status envelope (§V23); a database failure or any
unexpected error fails closed to a fixed, path/trace-free envelope via the shared
:func:`~arknights_mcp.mcp.tools._shared.run_guarded` guard.
"""

from __future__ import annotations

from arknights_mcp.mcp.envelopes import Provenance, ResponseEnvelope, error, ok
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._shared import (
    ConnectionProvider,
    observation_to_dict,
    run_guarded,
)
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.operators import CompareOperatorModulesInput
from arknights_mcp.services.module_compare import (
    ModuleCompareResult,
    ModuleComparison,
    ModuleLevelComparison,
    compare_operator_modules,
)

_TOOL_NAME = "compare_operator_modules"
_TOOL_TITLE = "Compare operator modules"
_TOOL_DESCRIPTION = (
    "Compare one Arknights operator's modules across the requested module levels "
    "(a subset of 1/2/3, default all three) by region + game_id. Each module lists "
    "its per-level stat bonuses, trait changes, talent changes, and upgrade cost "
    "side by side; a level a module does not define is marked present=false. mode "
    "facts_only returns the comparison; with_observations adds deterministic, "
    "evidence-backed observations (never a mandatory or best-in-slot verdict). "
    "en/cn are never mixed."
)

_NOT_FOUND_MESSAGE = "no operator matched the given region and game_id"
_NOT_FOUND_ACTION = (
    "verify the server and game_id (use search_entities to find it), or run "
    "`arknights-mcp status` to check the active build"
)


def _level_to_dict(level: ModuleLevelComparison) -> dict[str, object]:
    """One module level's change bundle (decoded structural JSON; §V16/§V18)."""
    return {
        "level": level.level,
        "present": level.present,
        "stat_bonus": level.stat_bonus,
        "trait_changes": level.trait_changes,
        "talent_changes": level.talent_changes,
        "cost": level.cost,
    }


def _module_to_dict(module: ModuleComparison) -> dict[str, object]:
    return {
        "game_id": module.game_id,
        "module_type": module.module_type,
        "display_name": module.display_name,
        "unlock_phase": module.unlock_phase,
        "unlock_level": module.unlock_level,
        "levels": [_level_to_dict(lv) for lv in module.levels],
    }


def _shape(result: ModuleCompareResult) -> ResponseEnvelope:
    """Map the domain result to a typed §V23 envelope (§V5 region + provenance)."""
    if result.status == "not_found" or result.provenance is None:
        return error("not_found", _NOT_FOUND_MESSAGE, suggested_action=_NOT_FOUND_ACTION)

    data: dict[str, object] = {
        "operator": {
            "server": result.server,
            "game_id": result.game_id,
            "display_name": result.operator_display_name,
        },
        "levels": list(result.levels),
        "mode": result.mode,
        "modules": [_module_to_dict(m) for m in result.modules],
    }
    if result.mode == "with_observations":
        # §V6 observations + §V26 warnings ride only in the analysis mode; the
        # analyzer version is stamped on the envelope (facts_only leaves it None).
        data["observations"] = [observation_to_dict(o) for o in result.observations]
        data["warnings"] = list(result.warnings)

    prov = result.provenance
    return ok(
        data,
        provenance=[
            Provenance(
                server=result.server,
                snapshot_id=prov.snapshot_id,
                imported_at=prov.imported_at,
            )
        ],
        analyzer_version=result.analyzer_version,
    )


def build_compare_operator_modules_spec(get_conn: ConnectionProvider) -> ToolSpec:
    """Build the ``compare_operator_modules`` :class:`ToolSpec` (§T45; §V14).

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build. The returned spec is read-only (§V2) for the single shared registry both
    transports dispatch from (§V14); its ``input_schema`` is the bounded model's
    JSON Schema, so the §V5 required ``server``, the §V18 ``game_id`` cap, the
    levels subset guard, and the ``mode`` enum land on the wire exactly as validated.
    """

    def handler(**params: object) -> ResponseEnvelope:
        # §V5/§V18 gate: the bounded model requires a region, caps the game_id, and
        # rejects an empty/out-of-range levels set or an unknown parameter *before*
        # any query runs -- a ValidationError propagates as a protocol-level rejection.
        parsed = CompareOperatorModulesInput.model_validate(params)
        return run_guarded(
            get_conn,
            lambda conn: compare_operator_modules(
                conn,
                server=parsed.server,
                game_id=parsed.game_id,
                levels=parsed.levels,
                mode=parsed.mode,
            ),
            _shape,
        )

    return ToolSpec(
        name=_TOOL_NAME,
        title=_TOOL_TITLE,
        description=_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(CompareOperatorModulesInput),
    )
