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
  lives in ``ranking`` rows that FOLD the raw drop facts + the derived
  ``sanity_per_item`` (§T161/§T176 -- the ranking subsumes the facts list, so the
  same entities are never listed twice).
  ``get_item_drops`` ranks the rows ascending by sanity per item (§V60) with the
  mandatory availability/first-clear/byproduct caveats on the observation, an ordering
  + evidence never a best-farm/mandatory verdict (§V7). A per-row confidence + typed
  deviation marker (``expired``/``flags``) appears only where a row deviates (thin
  sample / expired), with the explanatory sentence hoisted once onto the observation
  (§V85/B93); an expired cache downgrades that row below the §V8 threshold, never a
  fresh recommendation.
* **§V23** -- every result is a typed-status envelope; a database failure or any
  unexpected error fails closed to a fixed, path/trace-free envelope via the
  shared :func:`~arknights_mcp.mcp.tools._shared.run_guarded` guard.
"""

from __future__ import annotations

from arknights_mcp.analyzers import RankingRow
from arknights_mcp.mcp.envelopes import (
    Provenance,
    ResponseEnvelope,
    build_envelope,
    error,
)
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._enum_legend import (
    TOOL_ENUM_LEGEND_FIELDS,
    attach_enum_legend,
)
from arknights_mcp.mcp.tools._shared import (
    CONFIDENCE_SCALE_NOTE,
    ConnectionProvider,
    hoist_drop_provenance,
    page_to_dict,
    ranked_observation_to_dict,
    run_guarded,
)
from arknights_mcp.mcp.tools._stage_selector import (
    STAGE_SELECTOR_NOTE,
    stage_ambiguity_drop_hint,
    stage_ambiguity_limitation,
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
    "game_id, sourced from the Penguin Statistics cache. " + STAGE_SELECTOR_NOTE + " "
    "Each drop carries its "
    "quantity, its times, and its drop_rate. The times is the sample run count. The "
    "drop_rate is the expected number of items per run (quantity divided by times), not "
    "a probability. The penguin provenance (snapshot and "
    "fetched/expires time) shared by every drop is hoisted to a single drop_provenance "
    "block. A drop repeats a provenance field only when it differs. A drop carries "
    "expired:true only when it is past its cache expiry. Set include_efficiency to add "
    "deterministic farming observations (sanity spent per item). With that flag each "
    "rankable drop's facts fold into its ranked observation row, so no item is listed "
    "twice. A drop that cannot be ranked (missing drop rate) stays in the raw drops "
    "list with a warning naming it. If nothing was rankable the full raw drops list "
    "is returned with the warnings. Without the flag the "
    "raw drops list is returned. "
    "An item's rarity is its tier as an integer, 1 to 5, where 1 is T1. "
    "The response's enum_legend gives the values of item_type. Those are facts and "
    "observations only, never a best-farm or mandatory verdict. A drop past its expiry "
    "is still returned, flagged data_stale. A re-sync of the penguin source refreshes "
    "the cache. en/cn are never mixed."
)

#: §V106 (a): the stage itself does not exist -- a LOOKUP miss, so it stays an error.
_NOT_FOUND_MESSAGE = "no stage matched the given region and stage_code/game_id"
_NOT_FOUND_ACTION = (
    "verify the server and stage_code/game_id (use search_stages to find the stage), or "
    "ask the server admin to run `arknights-mcp sync --server all` to fetch the penguin "
    "drop cache"
)
#: §V106 (b)/B147: the stage RESOLVED and only the drop set is empty. That is a delivered
#: answer, so it ships ``ok`` + an empty ``drops`` with the why here instead of the
#: ``not_found`` that reported a well-formed question as a failed request. The retry
#: guidance is MOVED from the old ``suggested_action``, not dropped (§V111 b), and T195's
#: shared-stage_code alternates still ride alongside it -- on the 2026-07-28 build 206
#: shared codes have a first-pick stage with no drops beside a sibling that has them, so
#: the alternates are what stop the silent pick from inventing the absence (§V102).
_STAGE_NO_DROPS_LIMITATION = (
    "The Penguin Statistics cache lists no drops for this stage. That is what the cache "
    "records, not a claim that the stage drops nothing: a stage nobody has submitted runs "
    "for has no cached rates. Call get_data_status to check the cache's freshness, or ask "
    "the server admin to run `arknights-mcp sync --server all` to refresh it."
)
_STALE_LIMITATION = (
    "one or more drop rates are past their cache expiry; the figures are stale, not "
    "fresh -- re-sync the penguin drop source (`arknights-mcp sync`) to refresh them"
)


def _round_drop_rate(rate: float | None) -> float | None:
    """Round an emitted drop rate to 4dp (§V76; §V37 single home).

    Penguin ``quantity / times`` is a sample statistic whose 17-digit float ``repr``
    over-states its precision, so the raw float is never put on the wire; 4dp matches
    the sample's real significance (``sanity_per_item`` keeps its 2dp precedent,
    rounded upstream in the analyzer). A positive rate must stay positive: a rate
    below the 4dp step (e.g. ``1 / 100_000``) would round to ``0.0`` -- a value the
    analyzer treats as "does not drop" and excludes -- so it falls back to 4
    significant figures instead of a falsified zero on a ranked row. An absent rate
    (``None``) passes through unchanged. Shared by every drop-tool emit site so the
    rounding never diverges.
    """
    if rate is None:
        return None
    rounded = round(rate, 4)
    if rounded == 0.0 and rate > 0:
        return float(f"{rate:.4g}")
    return rounded


def _drop_identity(drop: DropFacts) -> dict[str, object]:
    """One item's typed drop identity + rate, WITHOUT the penguin provenance stamps.

    §V66.2: the ``snapshot_id`` / ``fetched_at`` / ``expires_at`` shared by every drop
    are hoisted to a single ``drop_provenance`` block by :func:`_shape`; a row repeats
    one only when it deviates, and carries ``expired`` only when it is past expiry.

    §V77/§V66 (B79): no per-drop ``region`` -- the response is single-region (``server``
    is a required selector, §V5), so region is stated ONCE on the parent ``stage`` +
    envelope provenance, never repeated on every row.

    §V67 (B98): an absent optional scalar is OMITTED, never emitted null -- the §V26
    warning naming a missing rate is the sole absence signal.
    """
    out: dict[str, object] = {"item_game_id": drop.item_game_id}
    optional: dict[str, object | None] = {
        "item_display_name": drop.item_display_name,
        "item_rarity": drop.item_rarity,
        "item_type": drop.item_type,
        "quantity": drop.quantity,
        "times": drop.times,
        "drop_rate": _round_drop_rate(drop.drop_rate),
    }
    out.update({k: v for k, v in optional.items() if v is not None})
    return out


def _drop_provenance_row(drop: DropFacts) -> dict[str, object]:
    """The penguin provenance stamps of one drop (the §V66.2 hoist input)."""
    return {
        "snapshot_id": drop.snapshot_id,
        "fetched_at": drop.fetched_at,
        "expires_at": drop.expires_at,
    }


def _apply_ranked_markers(
    out: dict[str, object],
    deviation: dict[str, object],
    *,
    expired: bool,
    row: RankingRow,
) -> dict[str, object]:
    """Apply the shared per-row deviation trailer to a folded ranking row (§V37).

    One home for the marker rules both fold emitters share: ``deviation`` carries only
    the provenance fields that deviate from the shared ``drop_provenance`` block
    (§V66.2); ``expired`` is emitted only when true (§V67, default = fresh) and doubles
    as the row's expired marker (one signal per condition, §V66); ``confidence`` +
    ``flags`` appear only where the row deviates from the observation-level baseline
    (§V66.1), with the sentence explaining each marker hoisted once onto the
    observation-level limitations (§V85/B93), never repeated per row.
    """
    out.update(deviation)
    if expired:
        out["expired"] = True
    if row.confidence is not None:
        out["confidence"] = row.confidence
    if row.flags:
        out["flags"] = list(row.flags)
    return out


def _efficiency_row(
    drop: DropFacts, deviation: dict[str, object], row: RankingRow
) -> dict[str, object]:
    """One ranking row that SUBSUMES the drop's raw facts (§T176/B95; §V66/§V55).

    The stage-view mirror of :func:`_item_efficiency_row` (§T161/B82): in efficiency
    mode the ranking is the SINGLE list for the RANKED items -- each ranked drop's raw
    facts fold into its ranking row rather than being duplicated in the sibling
    ``drops`` list (which listed the same items twice, B95). The row is exactly the
    :func:`_drop_identity` fields (the §V55 evidence -- ``drop_rate`` / ``times`` -- and
    the rarity/type identity) plus the derived ``sanity_per_item``. The stage-level
    ``sanity_cost`` is NOT repeated per row -- it rides the parent ``stage`` block once
    (§V66/§V77, unlike the item view where it varies per stage). ``drop`` is joined to
    ``row`` by id in :func:`_shape`, so the pairing can never silently drift with
    ordering.

    §V100/B134: the identity keys are emitted AS-IS -- ``item_game_id`` +
    ``item_display_name``. They used to be re-keyed to a generic ``id`` + ``name``, and
    the sibling ``get_item_drops`` ranking re-keyed a STAGE id and a stage CODE onto
    those same two names, so one shape carried flipped referents across two tools and an
    LLM mislabelled the very column it was rendering. Entity-prefixed keys make the
    referent readable from the key alone, so the re-key is simply gone, not remapped.
    """
    out = _drop_identity(drop)
    out["sanity_per_item"] = row.sanity_per_item
    return _apply_ranked_markers(out, deviation, expired=drop.expired, row=row)


def _shape(result: StageDropsResult) -> ResponseEnvelope:
    """Map the domain result to a typed §V23 envelope (§V5 region + provenance).

    ``ok`` and ``data_stale`` both deliver the drop facts (a stale drop is flagged,
    not withheld, §V53). ``not_found`` is now reserved for an absent STAGE -- a §V106 (a)
    lookup miss -- and fails to a §V24 error envelope with a suggested admin action; a
    stage that resolves with an empty drop cache is an ``ok`` carrying ``drops: []`` and
    the limitation that says why (§V106 b/B147). The efficiency observations
    ride only when ``include_efficiency`` produced them, each keeping its five §V6
    fields (§V55). Envelope provenance is the stage's game-data region attribution
    (§V5), distinct from the penguin drop chain (§V54).

    §V66.2 provenance hoist: every drop shares one penguin snapshot, so the
    ``snapshot_id`` / ``fetched_at`` / ``expires_at`` block is emitted once as
    ``drop_provenance`` and a drop repeats a field only where it deviates; a drop
    carries ``expired:true`` only when past its expiry (a fresh drop omits it), so the
    stale drop stays visible instead of buried in identical repeats.

    §T176/B95 (mirrors §T161/B82): the ranking SUBSUMES the ranked drop rows. With
    ``include_efficiency`` each RANKED drop's raw facts fold into its ranking row, so
    the response never lists the same item twice (§V66). A drop the analyzer could not
    rank (absent/non-positive ``drop_rate``) keeps its raw facts in the ``drops`` list
    alongside the §V26 warning that names it -- an unrankable fact (including its
    ``expired`` flag and provenance) is never silently withheld (§V53/§V47). Without
    the flag (or when nothing was rankable) all raw ``drops`` facts are emitted.
    """
    if result.status == "not_found" or result.stage is None:
        # §V106 (a): the only remaining ``not_found`` is a real LOOKUP miss -- no stage
        # under this region + selector. A stage that RESOLVES with an empty drop cache
        # now falls through to the ``ok`` path below (§V106 b/B147), which is also where
        # T195's shared-stage_code alternates moved: a code that matched nothing carries
        # no ambiguity by construction, so there are no alternates to name here.
        return error("not_found", _NOT_FOUND_MESSAGE, suggested_action=_NOT_FOUND_ACTION)

    shared_prov, deviations = hoist_drop_provenance([_drop_provenance_row(d) for d in result.drops])

    data: dict[str, object] = {
        "stage": {
            "server": result.stage.server,
            "game_id": result.stage.game_id,
            "stage_code": result.stage.stage_code,
            "display_name": result.stage.display_name,
            "sanity_cost": result.stage.sanity_cost,
        },
        "drop_provenance": shared_prov,
    }

    # §T176/B95: a ranked drop folds into its ranking row (below); only the residual --
    # every drop when there is no ranking, else just the unrankable ones -- is emitted
    # raw here, so no item ever appears twice AND no fact ever disappears (§V53/§V47).
    ranked_ids = (
        {row.id for row in result.observation.ranking} if result.observation is not None else set()
    )
    drops: list[dict[str, object]] = []
    for drop, deviation in zip(result.drops, deviations, strict=True):
        if drop.item_game_id in ranked_ids:
            continue
        row = _drop_identity(drop)
        row.update(deviation)  # §V66.2: only the fields that deviate from the shared block
        if drop.expired:
            row["expired"] = True  # §V67: emitted only when true (default = fresh)
        drops.append(row)
    # §V67/§V106 (b): an empty ``drops`` means two different things and the key has to
    # tell them apart. When the cache genuinely holds nothing for this stage the list is
    # emitted as ``[]`` -- a CONFIRMED none, the empty collection §V106 (b) requires
    # beside its limitation. When every drop folded into a ranking row instead, the facts
    # did not vanish, they moved, so the key is ABSENT rather than a false "none".
    if drops or not result.drops:
        data["drops"] = drops

    if result.analyzer_version is not None:
        # include_efficiency was requested: surface the §V66.1 single ranked
        # observation (§T129) + the analyzer's §V26 warnings (a missing sanity cost /
        # absent drop rate). When a ranking exists, its rows each fold the raw drop
        # facts (§T176/B95), joined to the facts by id so the pairing never rides an
        # ordering contract. The observation is omitted only when no drop was rankable
        # (then the raw ``drops`` above stay visible with the §V26 warnings).
        efficiency: dict[str, object] = {"warnings": list(result.warnings)}
        if result.observation is not None:
            by_id = {
                drop.item_game_id: (drop, deviation)
                for drop, deviation in zip(result.drops, deviations, strict=True)
            }
            merged = [_efficiency_row(*by_id[row.id], row) for row in result.observation.ranking]
            efficiency["observation"] = ranked_observation_to_dict(
                result.observation, ranking=merged
            )
        data["efficiency"] = efficiency

    # A stale result is a *delivered* fact flagged as aged, not a failed request, so
    # it keeps the full ``data`` (drops + optional efficiency) rather than the
    # ``{message}`` error-body shape -- the client reads the stale posture from the
    # per-drop ``expired`` flags + the staleness limitation (mirrors get_data_status).
    # build_envelope carries the status (ok | data_stale) + the full payload either
    # way, and still enforces the §V22 size cap.
    # §V102 (b)/B139: the shared-stage_code disclosure leads -- which stage these drop
    # rates belong to decides whether the numbers answer the question that was asked.
    limitations: tuple[str, ...] = stage_ambiguity_limitation(result.ambiguity)
    # §V106 (b)/B147: the stage resolved and the cache holds nothing for it. The why + the
    # next step ride a limitation on a delivered ``ok``, where they used to be the body of
    # a ``not_found``. The T195 alternates above stay AHEAD of it: which stage this empty
    # answer is about decides whether the emptiness answers the question that was asked.
    if not result.drops:
        # T195/§V102: when the code was shared, the alternates ride too -- the sibling
        # under the same code may be the stage that actually holds the drop data, and
        # that retry is the whole reason this disclosure exists.
        limitations = (
            *limitations,
            _STAGE_NO_DROPS_LIMITATION,
            *stage_ambiguity_drop_hint(result.ambiguity),
        )
    if result.stale:
        limitations = (*limitations, _STALE_LIMITATION)
    # §V104 (b)/(c): every drop row carries item_type, so its STATIC domain + the
    # source-defined "may grow" caveat ride the response rather than the description
    # (§V111 a) -- the domain is read against the token, post-call.
    limitations = attach_enum_legend(data, TOOL_ENUM_LEGEND_FIELDS[_TOOL_NAME], limitations)
    # §V104/§V6: the confidence scale rides the response carrying a confidence, once.
    if result.observation is not None:
        limitations = (*limitations, CONFIDENCE_SCALE_NOTE)

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
        limitations=limitations,
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
    "it. Each stage carries its sanity cost, quantity, times, and drop_rate. The times "
    "is the sample run count. The drop_rate is the expected number of items per run "
    "(quantity divided by times), not a probability. The penguin "
    "provenance (snapshot, fetched/expires time, and import time) shared by every stage "
    "is hoisted to a single drop_provenance block. A stage repeats a provenance field "
    "only when it differs. A stage carries expired:true only when past its cache expiry. "
    "Set include_efficiency to add deterministic farming observations, ranked ascending "
    "by sanity spent per item. With that flag the ranked observation is the single "
    "per-stage list, each row folding the stage facts and its sanity-per-item, so the "
    "stages are never listed twice. If nothing was rankable the raw stages list is "
    "returned with warnings naming the exclusions. Without the flag the raw stages list "
    "is returned and "
    "paged on its own. "
    "The item's rarity is its tier as an integer, 1 to 5, where 1 is T1. "
    "The response's enum_legend gives the values of item_type. "
    "That ranking is an ordering and evidence, never a "
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
# §V60/B91: a RESOLVED item with zero stage-drop cache is NOT an unknown-item miss -- it is
# a craft/synthesis-only (Workshop) or otherwise non-farmed material that has no stage drop
# to fetch, so an admin re-sync would add nothing. A DISTINCT sentence + a freshness
# self-check pointer (get_data_status, MCP-callable, §V71(a)) rather than the sync action,
# which would misread as "the cache is unsynced" for a row that will never exist.
# §V106 (b)/B147: since the item RESOLVED, the lookup succeeded and only the comparison set
# is empty, so this ships as a limitation on an ``ok`` envelope rather than as the body of
# a ``not_found``. B91's two-way split is untouched -- only the status moved; the wording
# and the get_data_status pointer are the same two facts, MOVED not deleted (§V111 b).
_ITEM_NO_DROPS_LIMITATION = (
    "This item exists but the Penguin Statistics cache lists no stage that drops it. It is "
    "obtained by synthesis (workshop) or another non-drop source, so there is no observed "
    "drop rate to report and a re-sync would not add one. Call get_data_status to confirm "
    "the penguin cache is fresh if you expected a drop."
)


def _item_stage_drop_identity(stage: ItemStageDropFacts) -> dict[str, object]:
    """One stage's drop of the item, WITHOUT the penguin provenance stamps (§V66.2).

    The ``snapshot_id`` / ``fetched_at`` / ``expires_at`` / ``imported_at`` shared by
    every stage are hoisted to a single ``drop_provenance`` block by :func:`_shape_item`;
    a row repeats one only when it deviates, and carries ``expired`` only when past expiry.

    §V77/§V66 (B79): no per-stage ``region`` -- an item's comparison is single-region
    (resolved PER region, §V5), so region is stated ONCE on the parent ``item`` +
    envelope provenance, never repeated on every stage row.

    §V67 (B98): an absent optional scalar is OMITTED, never emitted null -- the §V26
    warning naming a missing rate/cost is the sole absence signal.
    """
    out: dict[str, object] = {"stage_game_id": stage.stage_game_id}
    optional: dict[str, object | None] = {
        "stage_code": stage.stage_code,
        "sanity_cost": stage.sanity_cost,
        "quantity": stage.quantity,
        "times": stage.times,
        "drop_rate": _round_drop_rate(stage.drop_rate),
    }
    out.update({k: v for k, v in optional.items() if v is not None})
    return out


def _item_stage_provenance_row(stage: ItemStageDropFacts) -> dict[str, object]:
    """The penguin provenance stamps of one stage drop (the §V66.2 hoist input)."""
    return {
        "snapshot_id": stage.snapshot_id,
        "fetched_at": stage.fetched_at,
        "expires_at": stage.expires_at,
        "imported_at": stage.imported_at,
    }


def _item_efficiency_row(
    stage: ItemStageDropFacts, deviation: dict[str, object], row: RankingRow
) -> dict[str, object]:
    """One ranking row that SUBSUMES the stage's raw drop facts (§T161/B82; §V66/§V55).

    In efficiency mode the ranking is the SINGLE per-stage list -- the raw drop facts are
    folded into the ranking row rather than duplicated in a sibling ``stages`` list (which
    doubled the payload, B82). So this row carries both the §V55 evidence (``sanity_cost``
    / ``drop_rate`` / ``times`` -- the sample size) and the derived ``sanity_per_item``.
    ``stage`` is joined to ``row`` by id in :func:`_shape_item`, so the pairing can never
    silently drift with ordering.

    §V100/B134: the identity keys are emitted AS-IS -- ``stage_game_id`` +
    ``stage_code``. They used to be re-keyed to a generic ``id`` + ``name``, which was
    doubly wrong: the sibling ``get_stage_drops`` ranking put an ITEM id and an item
    display name under those same two names (flipped referents, one shape), and the
    ``name`` here was never a name at all -- ``"1-7"`` is the stage's CODE, not its
    display name ("The Tyrant"). §V100 requires a ``*_name`` key to carry a display name
    and a code to live in a ``*_code``, so the code keeps its own honest key and no
    ``*_name`` is invented for a value the comparison never loads.

    The row is the :func:`_item_stage_drop_identity` fields plus the derived figure and
    the shared :func:`_apply_ranked_markers` deviation trailer.
    """
    out = _item_stage_drop_identity(stage)
    out["sanity_per_item"] = row.sanity_per_item
    return _apply_ranked_markers(out, deviation, expired=stage.expired, row=row)


def _shape_item(result: ItemDropsResult) -> ResponseEnvelope:
    """Map the item-comparison domain result to a typed §V23 envelope (§V5/§V19/§V60).

    ``ok`` and ``data_stale`` both deliver the per-stage drop facts (an expired stage
    is flagged and downgraded, not dropped from the ranking, §V60). B91's two reasons
    stay split, now across two statuses (§V106): an UNKNOWN item (``result.item is
    None``) is a lookup miss -> ``not_found`` pointing at ``search_entities`` + an admin
    re-sync, while a RESOLVED item with zero stage-drop cache (a craft/synthesis-only
    material) is a set query that came back empty -> ``ok`` with ``stages: []`` and the
    distinct "exists but no observed drop" limitation pointing ``get_data_status`` (a
    freshness self-check), never the re-sync that would add no drop. The ranked efficiency
    observations ride only when
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

    §T161/B82: the ranking SUBSUMES the stage rows. With ``include_efficiency`` the
    single per-stage list is the ranked ``observation`` (each row folds the raw drop
    facts + its derived ``sanity_per_item``), so no separate ``stages`` list is emitted --
    the response never lists the same stages twice (~2x payload cut, §V66). Without the
    flag (or when nothing was rankable) the raw ``stages`` facts are emitted with their
    ``stages_page``. Whichever list is emitted is paged (§V22/§V19, B21).
    """
    if result.status == "not_found" or result.item is None:
        # §V106 (a): the only remaining ``not_found`` is an UNKNOWN item -- the game_id
        # resolves to no ``items`` row, so the lookup itself missed. A RESOLVED item with
        # an empty comparison falls through to the ``ok`` path below (§V106 b/B147),
        # keeping B91's distinct wording as a limitation there.
        return error("not_found", _ITEM_NOT_FOUND_MESSAGE, suggested_action=_ITEM_NOT_FOUND_ACTION)

    shared_prov, deviations = hoist_drop_provenance(
        [_item_stage_provenance_row(s) for s in result.stages]
    )

    data: dict[str, object] = {
        "item": {
            "server": result.item.server,
            "game_id": result.item.game_id,
            "display_name": result.item.display_name,
            "rarity": result.item.rarity,
            "item_type": result.item.item_type,
        },
        "drop_provenance": shared_prov,
    }

    # §T161/B82: when a ranking exists it subsumes the stage rows -- the service aligns
    # ``result.stages`` (and thus ``deviations``) 1:1 with the ranking rows, so each raw
    # fact folds into its ranking row and no separate ``stages`` list is emitted. Only
    # without a ranking (flag off, or nothing rankable) are the raw ``stages`` emitted.
    if result.observation is None:
        stages: list[dict[str, object]] = []
        for stage, deviation in zip(result.stages, deviations, strict=True):
            row = _item_stage_drop_identity(stage)
            row.update(deviation)  # §V66.2: only the fields that deviate from the shared block
            if stage.expired:
                row["expired"] = True  # §V67: emitted only when true (default = fresh)
            stages.append(row)
        data["stages"] = stages
        if result.stages_page is not None:
            data["stages_page"] = page_to_dict(result.stages_page)

    if result.analyzer_version is not None:
        # include_efficiency was requested: surface the §V66.1 single ranked observation
        # (§T129) + the §V26 warnings (a stage excluded for a missing sanity cost / drop
        # rate). When a ranking exists, its rows are this page of the global ranking, each
        # folding the raw drop facts (§T161/B82); the mandatory §V60 comparison caveats
        # ride the observation-level ``limitations`` so they travel with every page. The
        # observation is omitted only when no stage was rankable (then the raw ``stages``
        # above stay visible with the §V26 warnings, §V60).
        efficiency: dict[str, object] = {"warnings": list(result.warnings)}
        if result.observation is not None:
            # Joined by id (not position), so the fold can never silently mis-pair a
            # fact with another stage's figure if an ordering ever diverges upstream.
            by_id = {
                stage.stage_game_id: (stage, deviation)
                for stage, deviation in zip(result.stages, deviations, strict=True)
            }
            merged = [
                _item_efficiency_row(*by_id[row.id], row) for row in result.observation.ranking
            ]
            efficiency["observation"] = ranked_observation_to_dict(
                result.observation, ranking=merged
            )
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
    limitations: tuple[str, ...] = (_STALE_LIMITATION,) if result.stale else ()
    # §V106 (b)/B91: the item resolved and the comparison is empty. The distinct
    # craft/synthesis wording + the get_data_status pointer ride here now, where they used
    # to be a ``not_found`` body -- the split B91 asked for, one status later.
    if not result.stages:
        limitations = (*limitations, _ITEM_NO_DROPS_LIMITATION)
    # §V104 (b)/(c): the item block carries item_type, so its STATIC domain + the
    # source-defined "may grow" caveat ride the response, not the description (§V111 a).
    limitations = attach_enum_legend(data, TOOL_ENUM_LEGEND_FIELDS[_ITEM_TOOL_NAME], limitations)
    # §V104/§V6: the confidence scale rides the response carrying a confidence, once.
    if result.observation is not None:
        limitations = (*limitations, CONFIDENCE_SCALE_NOTE)

    return build_envelope(
        "data_stale" if result.stale else "ok",
        data=data,
        provenance=tuple(
            Provenance(server=result.server, snapshot_id=p.snapshot_id, imported_at=p.imported_at)
            for p in result.provenance
        ),
        limitations=limitations,
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
