"""Drop-rate services (§T91/§T103): the shared domain entry points both transports
call (§V14) for the penguin drop-rate cache, in both directions.

* :func:`get_stage_drops` (§T91) -- one *stage*'s drops. Loads the stage (region +
  provenance, §V5) and its penguin drop-rate cache (§V54), applies the §V53 stale
  check (``now`` past a drop's ``expires_at`` -> ``data_stale``, the drop still
  returned but flagged), and -- when ``include_efficiency`` is set -- runs the
  deterministic §T90 farming analyzer over the typed drop facts (§V55).
* :func:`get_item_drops` (§T103) -- one *item* compared *across* the stages that drop
  it (the §V60 reverse view). Resolves the item PER region (§V5), loads every stage
  that drops it with the stage's ``sanity_cost`` (§V55 input), applies the same §V53
  stale check per stage, and -- when ``include_efficiency`` is set -- runs the
  deterministic §T103 item-comparison analyzer, RANKED ascending by sanity per item
  (§V60). An expired stage's figure is downgraded to a limitation but KEPT in the
  ranking, not dropped (§V60).

The service adds no natural-language interpretation of its own; every emitted
observation keeps the five §V6 fields the analyzer stamped.

Read-only + parameterized SQL only (§V2): the parameterized ``SELECT``s live in
:class:`~arknights_mcp.db.repositories.stages.StageRepository` (the stage +
``sanity_cost``, reusing the shared ``_resolve_stage`` selector, §V37) and
:class:`~arknights_mcp.db.repositories.drops.DropRepository` (the drop cache, both
directions). It does not open the connection; callers pass one in, so both
transports share these exact functions (§V14). It never fetches penguin at query
time (§V52/§V1) -- it only reads the cache a CLI sync/import promoted.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

from arknights_mcp.analyzers.farming import (
    DropFact,
    FarmingContext,
    ItemFarmingContext,
    ItemStageDrop,
    RankedObservation,
    analyze_farming,
    analyze_item_farming,
)
from arknights_mcp.db.repositories.drops import (
    DropRepository,
    ItemRow,
    ItemStageDropRow,
    StageDropRow,
)
from arknights_mcp.db.repositories.stages import StageRepository
from arknights_mcp.models.common import PAGE_SIZE_DEFAULT
from arknights_mcp.services.stages import (
    SectionPage,
    StageFacts,
    StageProvenance,
    _resolve_stage,
    _section_page,
    _stage_facts,
    _validate_page,
)

#: Typed outcome of a drop lookup. ``data_stale`` is reported when at least one
#: served drop is past its ``expires_at`` (§V53); ``not_found`` when the stage is
#: absent OR the stage has no drop cache (§V24).
StageDropsStatus = Literal["ok", "data_stale", "not_found"]


@dataclass(frozen=True)
class DropFacts:
    """One item's typed drop fact for the wire (no prose; §V16/§V18/§V54).

    ``expired`` is this row's own §V53 verdict (``now`` past ``expires_at``); a
    stale drop is still returned, flagged rather than withheld. ``snapshot_id`` +
    ``fetched_at`` + ``expires_at`` are the penguin provenance chain (§V54).
    """

    item_game_id: str
    item_display_name: str | None
    item_rarity: str | None
    item_type: str | None
    region: str
    quantity: int | None
    times: int | None
    drop_rate: float | None
    snapshot_id: str
    fetched_at: str
    expires_at: str
    expired: bool


@dataclass(frozen=True)
class StageDropsResult:
    """Domain result of :func:`get_stage_drops`.

    Carries region + provenance on ``stage`` (§V5) and the penguin drop facts with
    their OWN provenance chain (§V54). ``observation`` is the SINGLE ranked farming
    observation (§V66.1/§T129), populated only when ``include_efficiency`` was
    requested (§V55) and at least one drop is rankable, else ``None``;
    ``analyzer_version`` is then the analyzer that produced it. ``stale`` mirrors the
    ``data_stale`` status so a caller need not re-scan the drops.
    """

    status: StageDropsStatus
    server: str
    stage: StageFacts | None
    drops: tuple[DropFacts, ...]
    observation: RankedObservation | None
    warnings: tuple[str, ...]
    analyzer_version: str | None
    stale: bool


#: Typed outcome of an item->stage drop comparison (§T103). ``data_stale`` when at
#: least one served stage drop is past its ``expires_at`` (§V53); ``not_found`` when
#: the item is absent OR has no drop cache in any stage (§V24/§V60).
ItemDropsStatus = Literal["ok", "data_stale", "not_found"]


@dataclass(frozen=True)
class ItemFacts:
    """The compared item's typed identity, region-attributed (§V5; §T103).

    No prose (§V16/§V18): identity + rarity/type only. The item is resolved PER
    region, so ``server`` is the item's own region and every ranked stage shares it
    (§V5 -- an item's comparison never mixes en + cn).
    """

    server: str
    game_id: str
    display_name: str | None
    rarity: str | None
    item_type: str | None


@dataclass(frozen=True)
class ItemStageDropFacts:
    """One stage's drop of the compared item for the wire (§V54/§V60).

    The reverse of :class:`DropFacts`: carries the STAGE identity + its ``sanity_cost``
    (the §V55 efficiency input) plus the drop's own penguin provenance chain
    (``snapshot_id`` + ``fetched_at`` + ``expires_at``, §V53/§V54). ``expired`` is this
    stage drop's own §V53 verdict; a stale figure is still returned, flagged rather
    than withheld (§V60 -- expired downgraded, never dropped).
    """

    stage_game_id: str
    stage_code: str | None
    sanity_cost: int | None
    region: str
    quantity: int | None
    times: int | None
    drop_rate: float | None
    snapshot_id: str
    fetched_at: str
    expires_at: str
    imported_at: str
    expired: bool


@dataclass(frozen=True)
class ItemDropsResult:
    """Domain result of :func:`get_item_drops` (§T103/§V60; §V19/§V22; §V66.1).

    Carries the region-attributed ``item`` (§V5) and the per-stage drop facts, each
    with its OWN penguin provenance chain (§V54). ``stages`` is the per-stage facts
    (ordered by ``stage_code`` then ``game_id``); ``observation`` is the SINGLE ranked
    (ascending sanity-per-item) efficiency comparison (§V66.1/§T129), populated only
    when ``include_efficiency`` was requested (§V55/§V60), with ``analyzer_version``
    then the analyzer that produced it. Its observation-level ``limitations`` carry the
    mandatory §V60 comparison caveats. ``stale`` mirrors the ``data_stale`` status.

    The reverse item->stage view is unbounded (a common material drops across
    ~100-200 stages), so the growable list is **paged** (§V22/§V19, B21). The two modes
    carry DIFFERENT per-stage shapes (§T161/B82 -- the ranking subsumes the stage rows
    so the response never lists the same stages twice):

    * ``include_efficiency`` OFF -- ``stages`` holds the requested ``stages_page`` of the
      raw per-stage drop facts (``stage_code`` order) and ``stages_page`` reports the full
      ``total`` + ``has_more``; ``observation`` / ``efficiency_page`` are ``None``.
    * ``include_efficiency`` ON -- the ranked ``observation`` is the SINGLE per-stage
      list. ``observation``'s ``ranking`` rows are the requested ``efficiency_page`` of
      the global ranking (``efficiency_page`` reports its ``total`` + ``has_more``), and
      ``stages`` carries the raw drop facts for exactly those ranking rows, in the SAME
      order (1:1, so the shaper folds each fact + its derived ``sanity_per_item`` into one
      row and emits no separate ``stages`` list); ``stages_page`` is ``None``. When no
      stage is rankable (every ``sanity_cost`` / ``drop_rate`` absent) the analyzer emits
      no observation, so ``stages`` falls back to the raw-facts page + ``stages_page`` so
      the drops stay visible (§V26 warnings name the excluded stages).

    The stale verdict, the ranked ORDER, and ``provenance`` are all computed over the
    FULL set BEFORE slicing, so page 1 is always the most-efficient N and ``data_stale``
    holds even when the expired stage falls on a later page. ``provenance`` is the
    distinct penguin snapshots (``snapshot_id`` + ``imported_at``) backing the full
    comparison, all sharing the item's region (§V5).
    """

    status: ItemDropsStatus
    server: str
    item: ItemFacts | None
    stages: tuple[ItemStageDropFacts, ...]
    stages_page: SectionPage | None
    observation: RankedObservation | None
    efficiency_page: SectionPage | None
    provenance: tuple[StageProvenance, ...]
    warnings: tuple[str, ...]
    analyzer_version: str | None
    stale: bool


def _is_expired(expires_at: str, now: datetime) -> bool:
    """True when ``now`` is at/past the drop cache's ``expires_at`` (§V53).

    An unparseable timestamp is treated as expired (fail-closed: an unreadable
    expiry is never presented as fresh, §V53). ``expires_at`` is stamped by the
    importer as an aware ISO string, but a naive value is normalized to UTC so the
    comparison never mixes naive/aware operands (mirrors ``status._age_days``).
    """
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return now >= parsed


def _drop_facts(row: StageDropRow, now: datetime) -> DropFacts:
    """Shape a repository row into the typed, region-attributed drop fact (§V5/§V54)."""
    return DropFacts(
        item_game_id=row.item_game_id,
        item_display_name=row.item_display_name,
        item_rarity=row.item_rarity,
        item_type=row.item_type,
        region=row.region,
        quantity=row.quantity,
        times=row.times,
        drop_rate=row.drop_rate,
        snapshot_id=row.snapshot_id,
        fetched_at=row.fetched_at,
        expires_at=row.expires_at,
        expired=_is_expired(row.expires_at, now),
    )


def _not_found(server: str) -> StageDropsResult:
    return StageDropsResult(
        status="not_found",
        server=server,
        stage=None,
        drops=(),
        observation=None,
        warnings=(),
        analyzer_version=None,
        stale=False,
    )


def get_stage_drops(
    conn: sqlite3.Connection,
    *,
    server: str,
    stage_code: str | None = None,
    game_id: str | None = None,
    include_efficiency: bool = False,
    now: datetime | None = None,
) -> StageDropsResult:
    """Fetch one stage's penguin drop-rate facts + optional farming efficiency (§T91).

    Selected by ``game_id`` (preferred, the unique key) or ``stage_code``.
    Read-only; parameterized SQL only (§V2); never a query-time penguin fetch
    (§V52). Returns a :class:`StageDropsResult` with region + provenance on the
    stage (§V5) and the drop facts with their own penguin provenance chain (§V54).

    A drop served past its ``expires_at`` is still returned but flagged, and the
    result status is ``data_stale`` (§V53) -- never presented as fresh. When
    ``include_efficiency`` is set, the deterministic §T90 farming analyzer runs
    over the typed drop facts (sanity_cost / drop_rate); an expired cache downgrades
    every figure to a limitation (§V55). ``now`` is injectable for deterministic
    expiry testing; it defaults to the current UTC time. Both transports call this
    same function (§V14).
    """
    clock = now if now is not None else datetime.now(tz=UTC)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)

    stage = _resolve_stage(StageRepository(conn), server, stage_code=stage_code, game_id=game_id)
    if stage is None:
        return _not_found(server)

    drop_rows = DropRepository(conn).drops_for_stage(stage.stage_pk)
    if not drop_rows:
        # A stage with no drop cache asserts no drop fact -- report it as absent with
        # a suggested admin action (§V24), rather than an empty ``ok`` that reads as
        # "this stage drops nothing". The tool maps this to a not_found envelope.
        return _not_found(server)

    facts = tuple(_drop_facts(row, clock) for row in drop_rows)
    stale = any(f.expired for f in facts)

    observation: RankedObservation | None = None
    warnings: tuple[str, ...] = ()
    analyzer_version: str | None = None
    if include_efficiency:
        # §V55: deterministic sanity-per-item over typed fields only. A stale cache
        # is passed through so every efficiency figure is downgraded to a limitation
        # (never a fresh recommendation, §V53). ``sample_size`` = penguin ``times``.
        # §V66.1: the analyzer emits ONE ranked observation over the stage's drops.
        analysis = analyze_farming(
            FarmingContext(
                server=stage.server,
                stage_code=stage.stage_code,
                sanity_cost=stage.sanity_cost,
                drops=tuple(
                    DropFact(
                        item_game_id=f.item_game_id,
                        item_display_name=f.item_display_name,
                        drop_rate=f.drop_rate,
                        sample_size=f.times,
                    )
                    for f in facts
                ),
                expired=stale,
            )
        )
        observation = analysis.observation
        warnings = analysis.warnings
        analyzer_version = analysis.analyzer_version

    return StageDropsResult(
        status="data_stale" if stale else "ok",
        server=stage.server,
        stage=_stage_facts(stage),
        drops=facts,
        observation=observation,
        warnings=warnings,
        analyzer_version=analyzer_version,
        stale=stale,
    )


# --- §T103/§V60: item -> stage drop comparison (reverse of get_stage_drops) ----


def _item_facts(item: ItemRow) -> ItemFacts:
    """Shape the resolved item row into the typed, region-attributed identity (§V5)."""
    return ItemFacts(
        server=item.server,
        game_id=item.game_id,
        display_name=item.display_name,
        rarity=item.rarity,
        item_type=item.item_type,
    )


def _item_stage_drop_facts(row: ItemStageDropRow, now: datetime) -> ItemStageDropFacts:
    """Shape a reverse-lookup row into the typed per-stage drop fact (§V5/§V54/§V60)."""
    return ItemStageDropFacts(
        stage_game_id=row.stage_game_id,
        stage_code=row.stage_code,
        sanity_cost=row.sanity_cost,
        region=row.region,
        quantity=row.quantity,
        times=row.times,
        drop_rate=row.drop_rate,
        snapshot_id=row.snapshot_id,
        fetched_at=row.fetched_at,
        expires_at=row.expires_at,
        imported_at=row.imported_at,
        expired=_is_expired(row.expires_at, now),
    )


def _item_not_found(server: str) -> ItemDropsResult:
    return ItemDropsResult(
        status="not_found",
        server=server,
        item=None,
        stages=(),
        stages_page=None,
        observation=None,
        efficiency_page=None,
        provenance=(),
        warnings=(),
        analyzer_version=None,
        stale=False,
    )


def _item_provenance(stages: tuple[ItemStageDropFacts, ...]) -> tuple[StageProvenance, ...]:
    """The distinct penguin snapshots backing the FULL comparison (§V5/§V54, B21).

    Derived over the whole stage set (never the current page) so paging never drops
    a snapshot from the provenance list. The comparison is region-scoped (§V5), so
    every stage shares the item's region; the distinct ``(snapshot_id, imported_at)``
    pairs are emitted in first-seen (already stage-ordered) order so the list is
    deterministic + reproducible (§V26). Typically one penguin snapshot per region.
    """
    seen: set[tuple[str, str]] = set()
    provenance: list[StageProvenance] = []
    for s in stages:
        key = (s.snapshot_id, s.imported_at)
        if key in seen:
            continue
        seen.add(key)
        provenance.append(StageProvenance(snapshot_id=s.snapshot_id, imported_at=s.imported_at))
    return tuple(provenance)


def get_item_drops(
    conn: sqlite3.Connection,
    *,
    server: str,
    game_id: str,
    include_efficiency: bool = False,
    stages_page: int = 1,
    stages_page_size: int = PAGE_SIZE_DEFAULT,
    efficiency_page: int = 1,
    efficiency_page_size: int = PAGE_SIZE_DEFAULT,
    now: datetime | None = None,
) -> ItemDropsResult:
    """Compare one item's drop-across-stages facts + optional ranked efficiency (§T103).

    The §V60 reverse of :func:`get_stage_drops`: the item is resolved by
    ``(server, game_id)`` PER region (§V5), then every stage that drops it is loaded
    with the stage's ``sanity_cost`` + ``stage_code`` + penguin provenance (§V54).
    Read-only; parameterized SQL only (§V2); never a query-time penguin fetch (§V52).

    An absent item, or an item with no drop cache in any stage, is a ``not_found``
    (§V24) -- the tool maps it to a suggested admin action, never a query-time
    download fallback. A stage drop served past its ``expires_at`` is still returned
    but flagged, and the status is ``data_stale`` (§V53) -- expired figures are
    downgraded, never dropped from the comparison (§V60). When ``include_efficiency``
    is set, the deterministic §T103 analyzer ranks the stages ascending by sanity per
    item (§V55/§V60), carrying the mandatory §V60 comparison caveats.

    The reverse view is unbounded (a common material drops across ~100-200 stages),
    so both growable lists are **paged** (§V22/§V19, B21): ``stages_page`` cursors the
    per-stage facts and ``efficiency_page`` the ranked observations, each its own
    cursor. Both are validated against the §V19 window here too (mirroring the model
    gate -- one contract, both places, never a silent clamp). The stale verdict, the
    ranked ORDER, and the provenance are computed over the FULL set BEFORE slicing, so
    page 1 is always the most-efficient N and ``data_stale`` holds even when the only
    expired stage falls on a later page. ``now`` is injectable for deterministic expiry
    testing; it defaults to the current UTC time. Both transports call this same
    function (§V14).
    """
    sp, ssize = _validate_page(stages_page, stages_page_size)
    ep, esize = _validate_page(efficiency_page, efficiency_page_size)

    clock = now if now is not None else datetime.now(tz=UTC)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)

    repo = DropRepository(conn)
    item = repo.item_by_game_id(server, game_id)
    if item is None:
        return _item_not_found(server)

    drop_rows = repo.drops_for_item(item.item_pk, server)
    if not drop_rows:
        # An item with no drop cache asserts no comparison -- report it absent with a
        # suggested admin action (§V24), never an empty ``ok``. The tool maps this to
        # a not_found envelope.
        return _item_not_found(server)

    # The FULL set drives the stale verdict + provenance + (below) the ranking, so
    # neither ever depends on which page was requested (§V22/§V19, B21).
    all_stages = tuple(_item_stage_drop_facts(row, clock) for row in drop_rows)
    stale = any(s.expired for s in all_stages)
    provenance = _item_provenance(all_stages)

    observation: RankedObservation | None = None
    efficiency_page_info: SectionPage | None = None
    warnings: tuple[str, ...] = ()
    analyzer_version: str | None = None
    # Default (efficiency OFF, or ON with nothing rankable): the raw per-stage facts
    # paged by their own cursor (§V22/§V19, B21).
    stages_page_info: SectionPage | None = _section_page(sp, ssize, len(all_stages))
    stages = all_stages[(sp - 1) * ssize : sp * ssize]

    if include_efficiency:
        # §V60: rank the stages ascending by sanity per item over typed fields only
        # (§V55). Each stage carries its OWN §V53 expiry so an expired figure is
        # downgraded per row (its own confidence + limitation) yet KEPT in the ranking,
        # not dropped. §V66.1: the analyzer emits ONE ranked observation; rank the FULL
        # set, THEN slice its ranking rows to the requested page -- page 1 is the
        # most-efficient N and the global order holds across pages (B21). The
        # observation-level §V6 fields + the §V60 comparison caveats ride every page.
        analysis = analyze_item_farming(
            ItemFarmingContext(
                server=item.server,
                item_game_id=item.game_id,
                drops=tuple(
                    ItemStageDrop(
                        stage_code=s.stage_code,
                        stage_game_id=s.stage_game_id,
                        sanity_cost=s.sanity_cost,
                        drop_rate=s.drop_rate,
                        sample_size=s.times,
                        expired=s.expired,
                    )
                    for s in all_stages
                ),
            )
        )
        full = analysis.observation
        warnings = analysis.warnings
        analyzer_version = analysis.analyzer_version
        if full is not None:
            # §T161/B82: the ranking SUBSUMES the stage rows -- the shaper folds each raw
            # fact + its derived sanity_per_item into ONE row, so the response never lists
            # the same stages twice (~2x payload, §V66). ``stages`` is realigned to the
            # ranking PAGE (same order, 1:1), and the separate stages page is dropped.
            page_rows = full.ranking[(ep - 1) * esize : ep * esize]
            observation = replace(full, ranking=page_rows)
            efficiency_page_info = _section_page(ep, esize, len(full.ranking))
            facts_by_id = {s.stage_game_id: s for s in all_stages}
            stages = tuple(facts_by_id[row.id] for row in page_rows)
            stages_page_info = None
        # else: no stage was rankable -- keep the raw-facts page above so the drops stay
        # visible (§V26 warnings name the excluded stages); no observation to subsume it.

    return ItemDropsResult(
        status="data_stale" if stale else "ok",
        server=item.server,
        item=_item_facts(item),
        stages=stages,
        stages_page=stages_page_info,
        observation=observation,
        efficiency_page=efficiency_page_info,
        provenance=provenance,
        warnings=warnings,
        analyzer_version=analyzer_version,
        stale=stale,
    )
