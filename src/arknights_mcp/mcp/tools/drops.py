"""``get_stage_drops`` + ``get_item_drops`` MCP tools (§T91/§T104; §V5/§V53/§V54/§V60; §I.tool).

Both tools bridge a bounded input model (§T30) to a shared
:mod:`~arknights_mcp.services.drops` service (§V14) and wrap the outcome in the
typed :class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope` (§T29). The two are
mirror images over the same penguin drop-rate cache: ``get_stage_drops`` lists one
stage's drops, ``get_item_drops`` compares one item across the stages that drop it
(the §V60 reverse view). Neither owns query logic -- only the model -> service ->
envelope mapping -- so both transports dispatch identical read-only (§V2) behaviour
from the single registry, and neither fetches penguin at query time (§V52/§V1);
they only read the promoted cache. They live in one module (§V37/§V38) since they
share the penguin provenance/expiry wire shaping.

The load-bearing invariants for both:

* **§V5** -- ``server`` is required, so every delivered fact is region-attributed +
  carries provenance; en/cn are never silently mixed.
* **§V53/§V54** -- each drop carries the penguin ``snapshot_id`` + ``fetched_at`` +
  ``expires_at`` (its OWN provenance chain, distinct from the game-data fact); a
  drop served past ``expires_at`` flips the status to ``data_stale`` and adds a
  staleness limitation -- never presented as fresh.
* **§V55/§V60** -- ``include_efficiency`` surfaces the deterministic farming
  observations, each carrying every §V6 field (rule_id + evidence + confidence +
  limitations + analyzer_version); ``get_item_drops`` ranks them ascending by sanity
  per item (§V60) with the mandatory availability/first-clear/byproduct caveats, an
  ordering + evidence never a best-farm/mandatory verdict (§V7). An expired cache
  downgrades a figure to a limitation, never a fresh recommendation.
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
from arknights_mcp.models.items import GetItemDropsInput
from arknights_mcp.models.stages import GetStageDropsInput
from arknights_mcp.services.drops import (
    DropFacts,
    ItemDropsResult,
    ItemStageDropFacts,
    StageDropsResult,
    get_item_drops,
    get_stage_drops,
)

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


# --- §T104/§V60: item -> stage drop comparison tool (reverse of get_stage_drops) --

_ITEM_TOOL_NAME = "get_item_drops"
_ITEM_TOOL_TITLE = "Get item drops"
_ITEM_TOOL_DESCRIPTION = (
    "Compare where one Arknights item drops by region + item game_id, sourced from "
    "the Penguin Statistics cache: the item's drop across every stage that yields it, "
    "each stage carrying its sanity cost, drop rate/sample size, and penguin "
    "provenance (snapshot + fetched/expires time). Set include_efficiency to add "
    "deterministic farming observations RANKED ascending by sanity spent per item -- "
    "an ordering and evidence, never a best-farm or mandatory verdict; stage "
    "availability, first-clear bonuses, and byproducts/synthesis are not modeled. A "
    "stage drop past its expiry is returned but flagged data_stale (downgraded, not "
    "dropped from the ranking); re-sync the penguin source to refresh. An item's "
    "comparison never mixes en/cn stages."
)

_ITEM_NOT_FOUND_MESSAGE = "no drop data matched the given region and item"
_ITEM_NOT_FOUND_ACTION = (
    "verify the server and item game_id, or run "
    "`arknights-mcp sync --server all` to fetch the penguin drop cache"
)


def _item_stage_drop_to_dict(stage: ItemStageDropFacts) -> dict[str, object]:
    """One stage's drop of the item + its penguin provenance stamps (§V54/§V60)."""
    return {
        "stage_game_id": stage.stage_game_id,
        "stage_code": stage.stage_code,
        "sanity_cost": stage.sanity_cost,
        "region": stage.region,
        "quantity": stage.quantity,
        "times": stage.times,
        "drop_rate": stage.drop_rate,
        "snapshot_id": stage.snapshot_id,
        "fetched_at": stage.fetched_at,
        "expires_at": stage.expires_at,
        "imported_at": stage.imported_at,
        "expired": stage.expired,
    }


def _shape_item(result: ItemDropsResult) -> ResponseEnvelope:
    """Map the item-comparison domain result to a typed §V23 envelope (§V5/§V60).

    ``ok`` and ``data_stale`` both deliver the per-stage drop facts (an expired stage
    is flagged and downgraded, not dropped from the ranking, §V60); ``not_found``
    (absent item or no drop cache in any stage) fails to a §V24 error envelope with a
    suggested admin action. The ranked efficiency observations ride only when
    ``include_efficiency`` produced them, each keeping its five §V6 fields (§V55),
    alongside the mandatory §V60 comparison caveats. Provenance is the item's own
    region attribution (§V5); each stage's penguin snapshot/expiry stamps travel in
    the stage rows themselves (§V54).
    """
    if result.status == "not_found" or result.item is None:
        return error("not_found", _ITEM_NOT_FOUND_MESSAGE, suggested_action=_ITEM_NOT_FOUND_ACTION)

    data: dict[str, object] = {
        "item": {
            "server": result.item.server,
            "game_id": result.item.game_id,
            "display_name": result.item.display_name,
            "rarity": result.item.rarity,
            "item_type": result.item.item_type,
        },
        "stages": [_item_stage_drop_to_dict(s) for s in result.stages],
    }
    if result.analyzer_version is not None:
        # include_efficiency was requested: surface the ranked §T103 observations, the
        # mandatory §V60 comparison caveats, and the §V26 warnings (a stage excluded
        # for a missing sanity cost / drop rate).
        data["efficiency"] = {
            "observations": [observation_to_dict(o) for o in result.observations],
            "limitations": list(result.limitations),
            "warnings": list(result.warnings),
        }

    # A stale result is a *delivered* comparison with one or more aged stage figures,
    # not a failed request, so it keeps the full ``data`` (stages + optional ranked
    # efficiency); the client reads the stale posture from the per-stage ``expired``
    # flags + the staleness limitation (mirrors get_stage_drops). The item is
    # penguin-sourced, so the envelope provenance is the distinct penguin snapshots
    # that backed the delivered drops (§V5); the item's comparison is region-scoped
    # (§V5), so every provenance row shares the item's region. The per-stage penguin
    # snapshot/expiry stamps still travel in the stage rows themselves (§V54).
    return build_envelope(
        "data_stale" if result.stale else "ok",
        data=data,
        provenance=_stage_provenance(result),
        limitations=(_STALE_LIMITATION,) if result.stale else (),
        analyzer_version=result.analyzer_version,
    )


def _stage_provenance(result: ItemDropsResult) -> tuple[Provenance, ...]:
    """The distinct penguin snapshots that backed the delivered drops (§V5/§V54).

    The comparison is region-scoped, so every row shares the item's region; the
    distinct ``(snapshot_id, imported_at)`` pairs are emitted in first-seen (already
    stage-ordered) order so the provenance list is deterministic + reproducible
    (§V26). Typically one penguin snapshot per region.
    """
    seen: set[tuple[str, str, str]] = set()
    provenance: list[Provenance] = []
    for s in result.stages:
        key = (s.region, s.snapshot_id, s.imported_at)
        if key in seen:
            continue
        seen.add(key)
        provenance.append(
            Provenance(server=s.region, snapshot_id=s.snapshot_id, imported_at=s.imported_at)
        )
    return tuple(provenance)


def build_get_item_drops_spec(get_conn: ConnectionProvider) -> ToolSpec:
    """Build the ``get_item_drops`` :class:`ToolSpec` (§T104; §V14).

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build. The returned spec is read-only (§V2) for the single shared registry both
    transports dispatch from (§V14); its ``input_schema`` is the bounded model's
    JSON Schema, so the §V5 required ``server`` + the ``game_id`` selector + the
    ``include_efficiency`` flag land on the wire exactly as validated.
    """

    def handler(**params: object) -> ResponseEnvelope:
        # §V5/§V18 gate: the bounded model requires a region + the item game_id, caps
        # the id length, and rejects an unknown parameter *before* any query runs -- a
        # ValidationError propagates as a protocol-level rejection.
        parsed = GetItemDropsInput.model_validate(params)
        return run_guarded(
            get_conn,
            lambda conn: get_item_drops(
                conn,
                server=parsed.server,
                game_id=parsed.game_id,
                include_efficiency=parsed.include_efficiency,
            ),
            _shape_item,
        )

    return ToolSpec(
        name=_ITEM_TOOL_NAME,
        title=_ITEM_TOOL_TITLE,
        description=_ITEM_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(GetItemDropsInput),
    )
