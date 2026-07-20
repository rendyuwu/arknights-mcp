"""``get_stage_drops`` MCP tool (§T91; §V5/§V53/§V54/§V55; §I.tool).

Bridges the bounded :class:`~arknights_mcp.models.stages.GetStageDropsInput`
(§T30) to the shared :func:`~arknights_mcp.services.drops.get_stage_drops` service
(§V14) and wraps the outcome in the typed
:class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope` (§T29). The tool owns no
query logic -- only the model -> service -> envelope mapping -- so both transports
dispatch identical read-only (§V2) behaviour from the single registry, and it
never fetches penguin at query time (§V52/§V1); it only reads the promoted cache.

Four invariants are load-bearing here:

* **§V5** -- ``server`` is required, so every ``ok`` drop is region-attributed +
  carries the stage's provenance; en/cn are never silently mixed.
* **§V53/§V54** -- each drop carries the penguin ``snapshot_id`` + ``fetched_at`` +
  ``expires_at`` (its OWN provenance chain, distinct from the game-data fact); a
  drop served past ``expires_at`` flips the status to ``data_stale`` and adds a
  staleness limitation -- never presented as fresh.
* **§V55** -- ``include_efficiency`` surfaces the deterministic §T90 farming
  observations, each carrying every §V6 field (rule_id + evidence + confidence +
  limitations + analyzer_version); an expired cache downgrades every figure to a
  limitation, never a fresh recommendation.
* **§V23** -- every result is a typed-status envelope; a database failure or any
  unexpected error fails closed to a fixed, path/trace-free envelope via the
  shared :func:`~arknights_mcp.mcp.tools._shared.run_guarded` guard.
"""

from __future__ import annotations

from arknights_mcp.mcp.envelopes import (
    Provenance,
    ResponseEnvelope,
    build_envelope,
    error,
)
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._shared import (
    ConnectionProvider,
    observation_to_dict,
    run_guarded,
)
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.stages import GetStageDropsInput
from arknights_mcp.services.drops import DropFacts, StageDropsResult, get_stage_drops

_TOOL_NAME = "get_stage_drops"
_TOOL_TITLE = "Get stage drops"
_TOOL_DESCRIPTION = (
    "Fetch one Arknights stage's item drop rates by region + stage_code (e.g. 4-4) "
    "or game_id, sourced from the Penguin Statistics cache: each drop carries its "
    "quantity/sample size, drop rate, and penguin provenance (snapshot + fetched/"
    "expires time). Set include_efficiency to add deterministic farming "
    "observations (sanity spent per item) -- facts and observations only, never a "
    "best-farm or mandatory verdict. A drop past its expiry is returned but flagged "
    "data_stale; re-sync the penguin source to refresh. en/cn are never mixed."
)

_NOT_FOUND_MESSAGE = "no drop data matched the given region and stage"
_NOT_FOUND_ACTION = (
    "verify the server and stage_code/game_id, or run "
    "`arknights-mcp sync --server all` to fetch the penguin drop cache"
)
_STALE_LIMITATION = (
    "one or more drop rates are past their cache expiry; the figures are stale, not "
    "fresh -- re-sync the penguin drop source (`arknights-mcp sync`) to refresh them"
)


def _drop_to_dict(drop: DropFacts) -> dict[str, object]:
    """One item's typed drop fact + its penguin provenance stamps (§V53/§V54)."""
    return {
        "item_game_id": drop.item_game_id,
        "item_display_name": drop.item_display_name,
        "item_rarity": drop.item_rarity,
        "item_type": drop.item_type,
        "region": drop.region,
        "quantity": drop.quantity,
        "times": drop.times,
        "drop_rate": drop.drop_rate,
        "snapshot_id": drop.snapshot_id,
        "fetched_at": drop.fetched_at,
        "expires_at": drop.expires_at,
        "expired": drop.expired,
    }


def _shape(result: StageDropsResult) -> ResponseEnvelope:
    """Map the domain result to a typed §V23 envelope (§V5 region + provenance).

    ``ok`` and ``data_stale`` both deliver the drop facts (a stale drop is flagged,
    not withheld, §V53); ``not_found`` (absent stage or no drop cache) fails to a
    §V24 error envelope with a suggested admin action. The efficiency observations
    ride only when ``include_efficiency`` produced them, each keeping its five §V6
    fields (§V55). Provenance is the stage's region attribution (§V5); the per-drop
    penguin snapshot/expiry stamps travel in the drop rows themselves (§V54).
    """
    if result.status == "not_found" or result.stage is None:
        return error("not_found", _NOT_FOUND_MESSAGE, suggested_action=_NOT_FOUND_ACTION)

    data: dict[str, object] = {
        "stage": {
            "server": result.stage.server,
            "game_id": result.stage.game_id,
            "stage_code": result.stage.stage_code,
            "display_name": result.stage.display_name,
            "sanity_cost": result.stage.sanity_cost,
        },
        "drops": [_drop_to_dict(d) for d in result.drops],
    }
    if result.analyzer_version is not None:
        # include_efficiency was requested: surface the §T90 observations + the
        # analyzer's §V26 warnings (a missing sanity cost / absent drop rate).
        data["efficiency"] = {
            "observations": [observation_to_dict(o) for o in result.observations],
            "warnings": list(result.warnings),
        }

    # A stale result is a *delivered* fact flagged as aged, not a failed request, so
    # it keeps the full ``data`` (drops + optional efficiency) rather than the
    # ``{message}`` error-body shape -- the client reads the stale posture from the
    # per-drop ``expired`` flags + the staleness limitation (mirrors get_data_status).
    # build_envelope carries the status (ok | data_stale) + the full payload either
    # way, and still enforces the §V22 size cap.
    prov = result.stage.provenance
    return build_envelope(
        "data_stale" if result.stale else "ok",
        data=data,
        provenance=(
            Provenance(
                server=result.stage.server,
                snapshot_id=prov.snapshot_id,
                imported_at=prov.imported_at,
            ),
        ),
        limitations=(_STALE_LIMITATION,) if result.stale else (),
        analyzer_version=result.analyzer_version,
    )


def build_get_stage_drops_spec(get_conn: ConnectionProvider) -> ToolSpec:
    """Build the ``get_stage_drops`` :class:`ToolSpec` (§T91; §V14).

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build. The returned spec is read-only (§V2) for the single shared registry
    both transports dispatch from (§V14); its ``input_schema`` is the bounded
    model's JSON Schema, so the §V5 required ``server`` + the exactly-one selector
    + the ``include_efficiency`` flag land on the wire exactly as validated.
    """

    def handler(**params: object) -> ResponseEnvelope:
        # §V5/§V18 gate: the bounded model requires a region + exactly one selector,
        # caps the id length, and rejects an unknown parameter *before* any query
        # runs -- a ValidationError propagates as a protocol-level rejection.
        parsed = GetStageDropsInput.model_validate(params)
        return run_guarded(
            get_conn,
            lambda conn: get_stage_drops(
                conn,
                server=parsed.server,
                stage_code=parsed.stage_code,
                game_id=parsed.game_id,
                include_efficiency=parsed.include_efficiency,
            ),
            _shape,
        )

    return ToolSpec(
        name=_TOOL_NAME,
        title=_TOOL_TITLE,
        description=_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(GetStageDropsInput),
    )
