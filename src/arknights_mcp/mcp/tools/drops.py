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
* **§V55/§V60/§V66.1** -- ``include_efficiency`` surfaces a SINGLE deterministic
  ranked farming observation (§T129): the §V6 fields (rule_id + confidence +
  analyzer_version) are stated once at the observation level and the per-entity data
  lives in ``ranking`` rows ``{id, name, sanity_per_item}`` whose ``id`` references
  the sibling drops/stages facts (evidence by reference, never re-copied numbers).
  ``get_item_drops`` ranks the rows ascending by sanity per item (§V60) with the
  mandatory availability/first-clear/byproduct caveats on the observation, an ordering
  + evidence never a best-farm/mandatory verdict (§V7). A per-row confidence +
  limitation appears only where a row deviates (thin sample / expired); an expired
  cache downgrades that row below the §V8 threshold, never a fresh recommendation.
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
    hoist_drop_provenance,
    page_to_dict,
    ranked_observation_to_dict,
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
    "Fetch one Arknights stage's item drop rates by region + stage_code (e.g. 4-4) or "
    "game_id, sourced from the Penguin Statistics cache. Each drop carries its "
    "quantity/sample size and drop rate. The penguin provenance (snapshot and "
    "fetched/expires time) shared by every drop is hoisted to a single drop_provenance "
    "block. A drop repeats a provenance field only when it differs. A drop carries "
    "expired:true only when it is past its cache expiry. Set include_efficiency to add "
    "deterministic farming observations (sanity spent per item). Those are facts and "
    "observations only, never a best-farm or mandatory verdict. A drop past its expiry "
    "is still returned, flagged data_stale. A re-sync of the penguin source refreshes "
    "the cache. en/cn are never mixed."
)

_NOT_FOUND_MESSAGE = "no drop data matched the given region and stage"
_NOT_FOUND_ACTION = (
    "verify the server and stage_code/game_id (use search_stages to find the stage), or "
    "ask the server admin to run `arknights-mcp sync --server all` to fetch the penguin "
    "drop cache"
)
_STALE_LIMITATION = (
    "one or more drop rates are past their cache expiry; the figures are stale, not "
    "fresh -- re-sync the penguin drop source (`arknights-mcp sync`) to refresh them"
)


def _drop_identity(drop: DropFacts) -> dict[str, object]:
    """One item's typed drop identity + rate, WITHOUT the penguin provenance stamps.

    §V66.2: the ``snapshot_id`` / ``fetched_at`` / ``expires_at`` shared by every drop
    are hoisted to a single ``drop_provenance`` block by :func:`_shape`; a row repeats
    one only when it deviates, and carries ``expired`` only when it is past expiry.
    """
    return {
        "item_game_id": drop.item_game_id,
        "item_display_name": drop.item_display_name,
        "item_rarity": drop.item_rarity,
        "item_type": drop.item_type,
        "region": drop.region,
        "quantity": drop.quantity,
        "times": drop.times,
        "drop_rate": drop.drop_rate,
    }


def _drop_provenance_row(drop: DropFacts) -> dict[str, object]:
    """The penguin provenance stamps of one drop (the §V66.2 hoist input)."""
    return {
        "snapshot_id": drop.snapshot_id,
        "fetched_at": drop.fetched_at,
        "expires_at": drop.expires_at,
    }


def _shape(result: StageDropsResult) -> ResponseEnvelope:
    """Map the domain result to a typed §V23 envelope (§V5 region + provenance).

    ``ok`` and ``data_stale`` both deliver the drop facts (a stale drop is flagged,
    not withheld, §V53); ``not_found`` (absent stage or no drop cache) fails to a
    §V24 error envelope with a suggested admin action. The efficiency observations
    ride only when ``include_efficiency`` produced them, each keeping its five §V6
    fields (§V55). Envelope provenance is the stage's game-data region attribution
    (§V5), distinct from the penguin drop chain (§V54).

    §V66.2 provenance hoist: every drop shares one penguin snapshot, so the
    ``snapshot_id`` / ``fetched_at`` / ``expires_at`` block is emitted once as
    ``drop_provenance`` and a drop repeats a field only where it deviates; a drop
    carries ``expired:true`` only when past its expiry (a fresh drop omits it), so the
    stale drop stays visible instead of buried in identical repeats.
    """
    if result.status == "not_found" or result.stage is None:
        return error("not_found", _NOT_FOUND_MESSAGE, suggested_action=_NOT_FOUND_ACTION)

    shared_prov, deviations = hoist_drop_provenance([_drop_provenance_row(d) for d in result.drops])
    drops: list[dict[str, object]] = []
    for drop, deviation in zip(result.drops, deviations, strict=True):
        row = _drop_identity(drop)
        row.update(deviation)  # §V66.2: only the fields that deviate from the shared block
        if drop.expired:
            row["expired"] = True  # §V67: emitted only when true (default = fresh)
        drops.append(row)

    data: dict[str, object] = {
        "stage": {
            "server": result.stage.server,
            "game_id": result.stage.game_id,
            "stage_code": result.stage.stage_code,
            "display_name": result.stage.display_name,
            "sanity_cost": result.stage.sanity_cost,
        },
        "drop_provenance": shared_prov,
        "drops": drops,
    }
    if result.analyzer_version is not None:
        # include_efficiency was requested: surface the §V66.1 single ranked
        # observation (§T129) + the analyzer's §V26 warnings (a missing sanity cost /
        # absent drop rate). The observation is omitted when no drop was rankable.
        efficiency: dict[str, object] = {"warnings": list(result.warnings)}
        if result.observation is not None:
            efficiency["observation"] = ranked_observation_to_dict(result.observation)
        data["efficiency"] = efficiency

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
    "Compare where one Arknights item drops by region + item game_id, sourced from the "
    "Penguin Statistics cache. It lists the item's drop across every stage that yields "
    "it. Each stage carries its sanity cost and drop rate/sample size. The penguin "
    "provenance (snapshot, fetched/expires time, and import time) shared by every stage "
    "is hoisted to a single drop_provenance block. A stage repeats a provenance field "
    "only when it differs. A stage carries expired:true only when past its cache expiry. "
    "Set include_efficiency to add deterministic farming observations, ranked ascending "
    "by sanity spent per item. That ranking is an ordering and evidence, never a "
    "best-farm or mandatory verdict. Stage availability, first-clear bonuses, and "
    "byproducts/synthesis are not modeled. A stage drop past its expiry is still "
    "returned, flagged data_stale. It is downgraded in the ranking, not dropped. A "
    "re-sync of the penguin source refreshes the cache. An item's comparison never "
    "mixes en/cn stages."
)

_ITEM_NOT_FOUND_MESSAGE = "no drop data matched the given region and item"
# §V73/§V71(a): the item game_id is now resolvable by name via search_entities (T142),
# so the pointer is honest -- no longer a dead-end (B67). Names an MCP-callable tool
# first, then the admin sync (phrased as an admin step, never a query-time download).
_ITEM_NOT_FOUND_ACTION = (
    "use search_entities to find the item's game_id by name, verify the server, or "
    "ask the server admin to run `arknights-mcp sync --server all` to fetch the "
    "penguin drop cache"
)


def _item_stage_drop_identity(stage: ItemStageDropFacts) -> dict[str, object]:
    """One stage's drop of the item, WITHOUT the penguin provenance stamps (§V66.2).

    The ``snapshot_id`` / ``fetched_at`` / ``expires_at`` / ``imported_at`` shared by
    every stage are hoisted to a single ``drop_provenance`` block by :func:`_shape_item`;
    a row repeats one only when it deviates, and carries ``expired`` only when past expiry.
    """
    return {
        "stage_game_id": stage.stage_game_id,
        "stage_code": stage.stage_code,
        "sanity_cost": stage.sanity_cost,
        "region": stage.region,
        "quantity": stage.quantity,
        "times": stage.times,
        "drop_rate": stage.drop_rate,
    }


def _item_stage_provenance_row(stage: ItemStageDropFacts) -> dict[str, object]:
    """The penguin provenance stamps of one stage drop (the §V66.2 hoist input)."""
    return {
        "snapshot_id": stage.snapshot_id,
        "fetched_at": stage.fetched_at,
        "expires_at": stage.expires_at,
        "imported_at": stage.imported_at,
    }


def _shape_item(result: ItemDropsResult) -> ResponseEnvelope:
    """Map the item-comparison domain result to a typed §V23 envelope (§V5/§V19/§V60).

    ``ok`` and ``data_stale`` both deliver the per-stage drop facts (an expired stage
    is flagged and downgraded, not dropped from the ranking, §V60); ``not_found``
    (absent item or no drop cache in any stage) fails to a §V24 error envelope with a
    suggested admin action. The ranked efficiency observations ride only when
    ``include_efficiency`` produced them, each keeping its five §V6 fields (§V55),
    alongside the mandatory §V60 comparison caveats. Envelope provenance is the item's
    own region attribution (§V5), derived over the FULL comparison in the service
    (never the current page, B21).

    §V66.2 provenance hoist: the penguin snapshot/fetch/expiry/import stamps shared by
    the stages on this page are emitted once as ``drop_provenance``; a stage repeats a
    field only where it deviates (a different snapshot) and carries ``expired:true``
    only when past its expiry (a fresh stage omits it), so a deviant/stale stage stays
    visible. The hoist is over the emitted page; the ranking + stale verdict +
    provenance were fixed over the full set upstream (B21), so a page never shifts them.

    Both growable lists are paged (§V22/§V19, B21): ``stages`` and ``observations``
    each carry their page and a ``*_page`` descriptor (``has_more``).
    """
    if result.status == "not_found" or result.item is None:
        return error("not_found", _ITEM_NOT_FOUND_MESSAGE, suggested_action=_ITEM_NOT_FOUND_ACTION)

    shared_prov, deviations = hoist_drop_provenance(
        [_item_stage_provenance_row(s) for s in result.stages]
    )
    stages: list[dict[str, object]] = []
    for stage, deviation in zip(result.stages, deviations, strict=True):
        row = _item_stage_drop_identity(stage)
        row.update(deviation)  # §V66.2: only the fields that deviate from the shared block
        if stage.expired:
            row["expired"] = True  # §V67: emitted only when true (default = fresh)
        stages.append(row)

    data: dict[str, object] = {
        "item": {
            "server": result.item.server,
            "game_id": result.item.game_id,
            "display_name": result.item.display_name,
            "rarity": result.item.rarity,
            "item_type": result.item.item_type,
        },
        "drop_provenance": shared_prov,
        "stages": stages,
    }
    if result.stages_page is not None:
        data["stages_page"] = page_to_dict(result.stages_page)
    if result.analyzer_version is not None:
        # include_efficiency was requested: surface the §V66.1 single ranked
        # observation (§T129) whose ``ranking`` rows are this page of the global
        # ranking, its page descriptor, and the §V26 warnings (a stage excluded for a
        # missing sanity cost / drop rate). The mandatory §V60 comparison caveats ride
        # the observation's observation-level ``limitations``, so they travel with every
        # page; the observation is omitted only when no stage was rankable.
        efficiency: dict[str, object] = {"warnings": list(result.warnings)}
        if result.observation is not None:
            efficiency["observation"] = ranked_observation_to_dict(result.observation)
        if result.efficiency_page is not None:
            efficiency["page"] = page_to_dict(result.efficiency_page)
        data["efficiency"] = efficiency

    # A stale result is a *delivered* comparison with one or more aged stage figures,
    # not a failed request, so it keeps the full ``data`` (stages + optional ranked
    # efficiency); the client reads the stale posture from the per-stage ``expired``
    # flags + the staleness limitation (mirrors get_stage_drops). The item is
    # penguin-sourced, so the envelope provenance is the distinct penguin snapshots
    # that backed the FULL comparison (§V5/§V54, derived in the service so a later page
    # never drops one); the comparison is region-scoped (§V5), so every provenance row
    # shares the item's region.
    return build_envelope(
        "data_stale" if result.stale else "ok",
        data=data,
        provenance=tuple(
            Provenance(server=result.server, snapshot_id=p.snapshot_id, imported_at=p.imported_at)
            for p in result.provenance
        ),
        limitations=(_STALE_LIMITATION,) if result.stale else (),
        analyzer_version=result.analyzer_version,
    )


def build_get_item_drops_spec(get_conn: ConnectionProvider) -> ToolSpec:
    """Build the ``get_item_drops`` :class:`ToolSpec` (§T104; §V14).

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build. The returned spec is read-only (§V2) for the single shared registry both
    transports dispatch from (§V14); its ``input_schema`` is the bounded model's
    JSON Schema, so the §V5 required ``server`` + the ``game_id`` selector + the
    ``include_efficiency`` flag land on the wire exactly as validated.
    """

    def handler(**params: object) -> ResponseEnvelope:
        # §V5/§V18/§V19 gate: the bounded model requires a region + the item game_id,
        # caps the id length, rejects an out-of-range page_size, and rejects an unknown
        # parameter *before* any query runs -- a ValidationError propagates as a
        # protocol-level rejection, never a silently widened page (§V19).
        parsed = GetItemDropsInput.model_validate(params)
        return run_guarded(
            get_conn,
            lambda conn: get_item_drops(
                conn,
                server=parsed.server,
                game_id=parsed.game_id,
                include_efficiency=parsed.include_efficiency,
                stages_page=parsed.stages_page.page,
                stages_page_size=parsed.stages_page.page_size,
                efficiency_page=parsed.efficiency_page.page,
                efficiency_page_size=parsed.efficiency_page.page_size,
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
