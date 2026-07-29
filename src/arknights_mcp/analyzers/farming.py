"""Farming-efficiency analyzer (§T90/§T103/§T129; §V6, §V8, §V26, §V55, §V7, §V60, §V66).

Deterministic, evidence-backed observations about the sanity cost of farming an
item. Pure and DB-free: two entry points share one math + conservatism core (§V37):

* :func:`analyze_farming` (§T90) -- one *stage*'s drops, driven by the
  ``get_stage_drops`` service (§T91) from the penguin drop-rate cache (§V53) plus the
  stage's ``sanity_cost``;
* :func:`analyze_item_farming` (§T103) -- one *item* compared *across* the stages
  that drop it, driven by the ``get_item_drops`` service (§T104), ranked ascending by
  sanity per item (lowest first). This is the §V60 reverse of the stage view; the
  ranking is an ordering + evidence, never a "best farm" / mandatory verdict (§V7).

There is no natural-language input -- every figure is computed from typed numeric
fields only (§V26/§V55), never from a name or description string.

The core figure is *sanity per item* = a stage's ``sanity_cost`` divided by the
item's ``drop_rate`` (expected drops per run): the average sanity spent to obtain
one of the item. It is computed in exactly one place (:func:`_efficiency`, §V37) so
the stage view and the item comparison can never diverge on the math or the
confidence ladder.

Emission shape (§V66.1/§T129): each entry point emits a SINGLE
:class:`RankedObservation` per rule rather than one observation per ranked entity.
The five §V6 fields (``rule_id`` / ``confidence`` / ``analyzer_version`` plus the
category/tag/title identity) are stated ONCE at the observation level; the per-entity
data lives in :class:`RankingRow` rows ``{id, name, sanity_per_item}`` ranked
ascending. A row's ``id`` REFERENCES the sibling drops/stages facts list the tool
already emits (which carries ``sanity_cost`` / ``drop_rate`` / ``sample_size``), so
those numbers are never re-copied onto the observation (evidence by reference, §V66.1
~85% token cut). Per-row ``confidence`` appears ONLY where a row deviates from the
observation-level baseline -- a thin sample or an expired cache -- so the deviant row
stays visible without restating the shared fields on every row.

Deviation caveats are stated once, not per row (§V85/B93): a deviating row carries a
short typed marker -- ``flags`` (:data:`FLAG_THIN_SAMPLE` / :data:`FLAG_SAMPLE_UNREPORTED`)
or ``expired`` -- and the SENTENCE explaining each condition present anywhere in the
ranking is hoisted ONCE onto the observation-level ``limitations`` by
:func:`_ranked_observation` (the §V37 single home shared by both views). Twenty thin
rows no longer repeat the identical "below the floor" sentence twenty times; each
carries ``flags=("thin_sample",)`` + its own reduced confidence, and the sentence
appears once. The §V6 discipline is intact: the observation is fully attributed, the
caveat content is complete at the observation level, and every deviating figure still
carries its own reduced confidence.

Conservatism (§V8/§V55):

* a drop sample below :data:`SAMPLE_SIZE_FLOOR` runs -> the row's confidence is
  reduced, the row is flagged, and an observation-level limitation records the thin
  sample once (the rate is noisy, §V55 extends §V8; §V85 hoist);
* an expired drop cache (§V53) -> the row's figure is downgraded (marked ``expired``,
  caveat hoisted once) and its confidence forced below the §V8 recommendation
  threshold, never presented as a fresh recommendation (§V55);
* a missing ``sanity_cost`` or an absent/zero ``drop_rate`` yields no row -- it is a
  §V26 warning, never a fabricated or divide-by-zero conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass

from arknights_mcp.analyzers.base import ANALYZER_VERSION

RULE_ID = "farming.sanity_per_item"
_CATEGORY = "farming"
_TAG = "sanity_per_item"
_TITLE = "Farming sanity cost per item"

#: A drop sample of at least this many runs is treated as a stable rate; below it the
#: rate is noisy, so confidence is reduced and a limitation is recorded (§V8/§V55).
SAMPLE_SIZE_FLOOR = 100

#: Confidence when the drop rate rests on a sufficient sample and the cache is fresh:
#: the figure is a deterministic ratio of two typed numeric fields (§V26), so it is
#: high but never certain (sampling variance remains). This is the observation-level
#: BASELINE (§V66.1): a ranking row that matches it is non-deviating and omits its own
#: confidence/limitation; a thin/expired row deviates below it.
_CONF_STABLE = 0.8
#: Confidence when the sample is below the floor -- kept under the §V8 threshold so a
#: thin-sample figure is a limitation, not a recommendation (§V55).
_CONF_THIN_SAMPLE = 0.3
#: Confidence when the drop cache is expired -- forced under the §V8 threshold so an
#: expired figure is never served as a fresh recommendation (§V53/§V55).
_CONF_EXPIRED = 0.3

#: §V8 recommendation threshold: an observation at or above 0.5 may read as a
#: recommendation; below it must be reported as a limitation.
_RECOMMENDATION_THRESHOLD = 0.5

# §V8 self-check: the conservative confidences must stay strictly below the
# recommendation threshold and the stable confidence at/above it, so a future tune of
# any _CONF_* constant across 0.5 fails loudly here rather than silently flipping a
# thin-sample/expired figure into a recommendation (the §V8 boundary is executable,
# not just documented).
assert _CONF_THIN_SAMPLE < _RECOMMENDATION_THRESHOLD <= _CONF_STABLE
assert _CONF_EXPIRED < _RECOMMENDATION_THRESHOLD

#: Typed per-row deviation flags (§V85/B93). A deviating row carries the short flag;
#: the sentence explaining the condition is hoisted ONCE onto the observation-level
#: limitations (never repeated verbatim on every deviating row). The two sample flags
#: are mutually exclusive (a sample is either reported-but-thin or unreported).
FLAG_THIN_SAMPLE = "thin_sample"
FLAG_SAMPLE_UNREPORTED = "sample_unreported"

#: The §V85 hoisted sentences, one per deviation condition, in the deterministic order
#: they are appended to the observation-level limitations when the condition is present
#: anywhere in the ranking. Client-facing wording (§V71): each names the per-row marker
#: (the ``expired`` field / the ``flags`` value) the client joins it back on.
_HOISTED_LIMITATIONS: tuple[tuple[str, str], ...] = (
    (
        "expired",
        "rows marked expired are past the drop cache expiry; their figures are "
        "downgraded, never a fresh recommendation -- re-sync the penguin drop source "
        "to refresh them",
    ),
    (
        FLAG_THIN_SAMPLE,
        f"rows flagged {FLAG_THIN_SAMPLE} rest on a drop sample below the "
        f"{SAMPLE_SIZE_FLOOR}-run floor; the rate is noisy and the figure uncertain",
    ),
    (
        FLAG_SAMPLE_UNREPORTED,
        f"rows flagged {FLAG_SAMPLE_UNREPORTED} have no reported sample size; "
        "rate stability is unverified",
    ),
)


@dataclass(frozen=True)
class RankingRow:
    """One ranked entity in a compacted farming observation (§V66.1/§T129).

    ``id`` and ``name`` are INTERNAL slots, not wire keys. ``id`` is the entity's stable
    reference into the SIBLING facts list the tool already emits -- a drop's
    ``item_game_id`` in the stage view, a stage's ``stage_game_id`` in the item comparison
    (§V68/B57: the UNAMBIGUOUS id, never the ``stage_code`` alone, which the normal + tough
    variants share) -- so a client joins on it to read the ``sanity_cost`` /
    ``drop_rate`` / ``sample_size`` carried there; those numbers are NOT re-copied here
    (§V66.1). ``name`` is the display label the tool shows alongside: the item's display
    name in the stage view, the stage's ``stage_code`` in the item comparison.

    §V100/B134: these two generic names must NOT reach the wire. They used to -- both
    emitters re-keyed onto a bare ``id`` + ``name``, so one shape carried an ITEM id and
    an item display name in one tool and a STAGE id and a stage CODE in its sibling, and
    a client rendering the table mislabelled its own column. Each emitter now emits the
    entity-prefixed keys instead (``item_game_id`` / ``item_display_name`` and
    ``stage_game_id`` / ``stage_code``), so the referent is readable from the key. The
    slots stay generic here because this dataclass is the ranking's shared analyzer-side
    shape; the naming rule belongs to the emit site, and a contract test walks every
    registered tool's ``ranking`` rows to keep it there.
    ``sanity_per_item`` is the derived ranking figure (not present in the sibling
    list). ``confidence`` is populated ONLY when this row deviates from the
    observation-level baseline (:data:`_CONF_STABLE`) -- a thin sample or an expired
    cache -- so a non-deviating row stays the minimal fields and a deviant one carries
    its own reduced §V8/§V55 confidence. The deviation CAUSE rides as a short typed
    marker (§V85/B93): ``expired`` for a stale cache, ``flags`` for a
    thin/unreported sample; the sentence explaining each condition is hoisted once
    onto the observation-level limitations, never repeated per row.
    """

    id: str
    name: str | None
    sanity_per_item: float
    confidence: float | None = None
    expired: bool = False
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RankedObservation:
    """One compacted, evidence-backed farming observation over a ranked set (§V66.1/§V6).

    Replaces the pre-v0.2 "one observation per entity" emission (§T129). The §V6
    identity fields (``rule_id`` / ``confidence`` / ``analyzer_version``) are stated
    ONCE here; the per-entity data lives in ``ranking`` rows whose ``id`` references
    the sibling facts list (evidence by reference, never a re-copied number, §V66.1).
    The ranking is ascending by ``sanity_per_item`` -- an ordering + evidence, never a
    "best farm" / mandatory verdict (§V7/§V55). ``confidence`` is the baseline for a
    fresh, well-sampled figure (:data:`_CONF_STABLE`); a row that deviates (thin sample
    / expired) carries its OWN lower confidence + a typed marker. ``limitations`` here
    are the caveats that apply to the ranking: the §V60 comparison caveats for the item
    view, plus the §V85 hoisted sentence for each deviation condition present anywhere
    in the ranking (stated once, joined back to rows via their ``expired``/``flags``
    markers).
    """

    rule_id: str
    category: str
    tag: str
    title: str
    summary: str
    confidence: float
    ranking: tuple[RankingRow, ...]
    limitations: tuple[str, ...] = ()
    analyzer_version: str = ANALYZER_VERSION


@dataclass(frozen=True)
class DropFact:
    """One item's typed penguin drop fact for a stage (rule input; §V53/§V54).

    ``drop_rate`` is the expected quantity per run (penguin ``quantity / times``);
    ``sample_size`` is penguin ``times`` (the number of sampled runs). ``None`` on a
    field means the datum was absent, so the analyzer reduces confidence or records a
    limitation (§V26), never silently treats it as zero.
    """

    item_game_id: str
    item_display_name: str | None
    drop_rate: float | None
    sample_size: int | None


@dataclass(frozen=True)
class FarmingContext:
    """Typed input to the farming analyzer for one stage's drops.

    ``expired`` reflects the §V53 stale check the service applies (``now`` past the
    drop cache's ``expires_at``); when true, every efficiency figure is downgraded to
    a limitation (§V55). ``sanity_cost is None`` means the stage did not record a
    sanity cost -- the analyzer cannot compute a per-item cost and warns (§V26).
    """

    server: str
    stage_code: str | None
    sanity_cost: int | None
    drops: tuple[DropFact, ...]
    expired: bool = False


@dataclass(frozen=True)
class FarmingAnalysis:
    """Aggregate result of running the farming rule over one stage's drops (§V6/§V66.1).

    ``observation`` is the SINGLE ranked observation (§T129) over the stage's drops, or
    ``None`` when no drop is rankable (a missing sanity cost, or every drop rate absent
    -- the reason rides ``warnings``, §V26).
    """

    server: str
    stage_code: str | None
    observation: RankedObservation | None
    warnings: tuple[str, ...]
    analyzer_version: str = ANALYZER_VERSION


@dataclass(frozen=True)
class _Efficiency:
    """The computed sanity-per-item figure plus its §V8/§V55 confidence + flags.

    The single §V37 home's output: the farming math and the conservatism ladder are
    computed once (:func:`_efficiency`) so the stage view (:func:`analyze_farming`) and
    the item comparison (:func:`analyze_item_farming`) can never diverge on the figure
    or on how a thin/absent sample or an expired cache lowers confidence. ``flags`` are
    the typed sample markers (§V85); the explanatory sentences live once at the
    observation level (:func:`_ranked_observation`), never here per figure.
    """

    sanity_per_item: float
    confidence: float
    flags: tuple[str, ...]


def _efficiency(
    *, sanity_cost: int, drop_rate: float, sample_size: int | None, expired: bool
) -> _Efficiency:
    """Compute sanity per item + the §V8/§V55 confidence/flags (§V37 single home).

    Caller guarantees a positive ``sanity_cost`` and a positive ``drop_rate`` (a
    missing/zero either is a §V26 warning handled upstream), so the ratio is always
    well-defined. Each conservatism condition INDEPENDENTLY lowers confidence (lowest
    wins) and leaves its own typed marker, so a figure that is both expired AND
    thin-sampled carries BOTH caveats -- §V6 wants complete limitations, not a
    dominant-cause-only note (§V8/§V53/§V55); the expired marker rides the row's
    ``expired`` field, the sample marker its ``flags`` (§V85). A fresh, well-sampled
    rate keeps the stable confidence.
    """
    sanity_per_item = sanity_cost / drop_rate

    flags: list[str] = []
    confidence = _CONF_STABLE
    if expired:
        confidence = min(confidence, _CONF_EXPIRED)
    if sample_size is not None and sample_size < SAMPLE_SIZE_FLOOR:
        confidence = min(confidence, _CONF_THIN_SAMPLE)
        flags.append(FLAG_THIN_SAMPLE)
    if sample_size is None:
        # A rate with no reported sample size is unverifiable for stability (§V26).
        confidence = min(confidence, _CONF_THIN_SAMPLE)
        flags.append(FLAG_SAMPLE_UNREPORTED)

    return _Efficiency(
        sanity_per_item=sanity_per_item,
        confidence=confidence,
        flags=tuple(flags),
    )


def _ranking_row(
    *,
    entity_id: str,
    name: str | None,
    sanity_cost: int,
    drop_rate: float,
    sample_size: int | None,
    expired: bool,
) -> tuple[float, RankingRow]:
    """Build one ranking row + its ascending sort key (§V66.1/§V6/§V37).

    Caller guarantees a positive ``sanity_cost`` and ``drop_rate``. The figure +
    confidence + flags come from the shared :func:`_efficiency` core (§V37); this
    only shapes them into a :class:`RankingRow`. The row omits its ``confidence`` when
    it matches the observation-level baseline (:data:`_CONF_STABLE`) and carries it
    only where the row DEVIATES (thin sample / expired), so a non-deviating row stays
    the minimal ``{id, name, sanity_per_item}`` (§V66.1). The deviation cause is the
    row's typed ``expired``/``flags`` marker; its explanatory sentence is hoisted once
    by :func:`_ranked_observation` (§V85). The sibling facts list holds
    ``sanity_cost`` / ``drop_rate`` / ``sample_size``; ``entity_id`` references them
    rather than re-copying (§V66.1). The returned float is the sanity-per-item used to
    rank ascending (§V60).
    """
    eff = _efficiency(
        sanity_cost=sanity_cost,
        drop_rate=drop_rate,
        sample_size=sample_size,
        expired=expired,
    )
    # Confidence is the single deviation signal: every flag-appending branch in
    # `_efficiency` also lowers confidence, and the module asserts pin the reduced
    # constants strictly below `_CONF_STABLE`, so flags can never appear alone.
    deviates = eff.confidence != _CONF_STABLE
    row = RankingRow(
        id=entity_id,
        name=name,
        sanity_per_item=round(eff.sanity_per_item, 2),
        confidence=eff.confidence if deviates else None,
        expired=expired,
        flags=eff.flags,
    )
    return eff.sanity_per_item, row


def _ranked_observation(
    *, summary: str, ranking: tuple[RankingRow, ...], limitations: tuple[str, ...]
) -> RankedObservation:
    """Assemble the single ranked observation with the §V6 fields once (§V66.1/§V6).

    The §V85/B93 hoist lives here (§V37 single home for both views): each deviation
    condition present anywhere in the ranking appends its explanatory sentence ONCE to
    the observation-level ``limitations`` (after the caller's ranking-wide caveats), in
    the fixed :data:`_HOISTED_LIMITATIONS` order so the output is deterministic. Rows
    carry only the typed ``expired``/``flags`` markers + their own reduced confidence;
    a condition absent from every row emits no sentence.
    """
    present = {flag for row in ranking for flag in row.flags}
    if any(row.expired for row in ranking):
        present.add("expired")
    hoisted = tuple(sentence for marker, sentence in _HOISTED_LIMITATIONS if marker in present)
    return RankedObservation(
        rule_id=RULE_ID,
        category=_CATEGORY,
        tag=_TAG,
        title=_TITLE,
        summary=summary,
        confidence=_CONF_STABLE,
        ranking=ranking,
        limitations=limitations + hoisted,
    )


_STAGE_SUMMARY = (
    "Sanity spent per item for each drop on this stage, computed as the stage sanity "
    "cost divided by the expected drops per run; items are ordered from the lowest "
    "sanity per copy."
)


def analyze_farming(ctx: FarmingContext) -> FarmingAnalysis:
    """Run the deterministic farming-efficiency rule over ``ctx`` (§V6/§V26/§V55/§V66.1).

    Emits a SINGLE ranked observation (§T129) whose ``ranking`` rows cover the stage's
    drops, ordered ascending by sanity per item (tie-broken by ``item_game_id`` so the
    output is deterministic, §V26/§V60). A stage with no ``sanity_cost`` warns once and
    produces no observation (§V26); a drop with an absent or non-positive ``drop_rate``
    warns per item, never fabricating a figure or dividing by zero. The §V6 fields are
    stated once at the observation level; a row deviates (its own confidence + typed
    ``expired``/``flags`` marker) only for a thin sample or an expired cache, with the
    explanatory sentence hoisted once onto the observation (§V85). The analyzer adds no
    prescriptive language (§V7).
    """
    warnings: list[str] = []

    if ctx.sanity_cost is None or ctx.sanity_cost <= 0:
        warnings.append(
            "stage sanity cost is missing or non-positive; farming efficiency not computed"
        )
        return FarmingAnalysis(
            server=ctx.server,
            stage_code=ctx.stage_code,
            observation=None,
            warnings=tuple(warnings),
        )

    rows: list[tuple[float, str, RankingRow]] = []
    for drop in sorted(ctx.drops, key=lambda d: d.item_game_id):
        if drop.drop_rate is None or drop.drop_rate <= 0:
            warnings.append(
                f"{drop.item_game_id}: drop rate is missing or non-positive; "
                "sanity-per-item not computed"
            )
            continue
        key, row = _ranking_row(
            entity_id=drop.item_game_id,
            name=drop.item_display_name,
            sanity_cost=ctx.sanity_cost,
            drop_rate=drop.drop_rate,
            sample_size=drop.sample_size,
            expired=ctx.expired,
        )
        # Rank ascending by sanity/item, tie-broken by item id so equal-cost items rank
        # deterministically (§V26/§V60).
        rows.append((key, drop.item_game_id, row))

    rows.sort(key=lambda r: (r[0], r[1]))
    ranking = tuple(row for _, _, row in rows)

    observation = (
        _ranked_observation(summary=_STAGE_SUMMARY, ranking=ranking, limitations=())
        if ranking
        else None
    )
    return FarmingAnalysis(
        server=ctx.server,
        stage_code=ctx.stage_code,
        observation=observation,
        warnings=tuple(warnings),
    )


# --- §T103/§V60: item -> stage comparison (reverse of the stage view) ----------

#: The §V60 comparison-wide caveats every item->stage ranking MUST carry: the drop
#: cache models neither a stage's open/closed availability (an event stage is not
#: permanently farmable) nor the one-time first-clear bonus, and byproduct /
#: synthesis-recipe routes to the item are not modeled. The ranking is
#: sanity-per-direct-drop only; the decision stays the user's (§V7). Under §V66.1
#: these ride the single observation's observation-level ``limitations`` (they apply to
#: the whole ranking), stated once rather than per row.
_ITEM_COMPARISON_LIMITATIONS: tuple[str, ...] = (
    "stage availability is not modeled: an event stage may be closed and not currently "
    "farmable; the drop cache does not track open/closed windows",
    "the one-time first-clear bonus is not modeled; only the repeatable drop rate is",
    "byproduct and synthesis-recipe routes to this item are not modeled; only direct "
    "stage drops are ranked",
)

_ITEM_SUMMARY = (
    "Sanity spent per copy of this item across the stages that drop it, computed as "
    "each stage's sanity cost divided by the expected drops per run; stages are ordered "
    "from the lowest sanity per copy."
)


@dataclass(frozen=True)
class ItemStageDrop:
    """One stage's penguin drop fact for a fixed item (rule input; §T103/§V60).

    The reverse of :class:`DropFact`: there the item varies for a fixed stage; here the
    stage varies for a fixed item. ``sanity_cost`` is this stage's cost per run,
    ``drop_rate`` the expected quantity per run, ``sample_size`` penguin ``times``, and
    ``expired`` this stage's own §V53 verdict. ``None`` on a field means the datum was
    absent, so the analyzer excludes the stage with a §V26 warning, never a fabricated
    zero.
    """

    stage_code: str | None
    stage_game_id: str
    sanity_cost: int | None
    drop_rate: float | None
    sample_size: int | None
    expired: bool = False


@dataclass(frozen=True)
class ItemFarmingContext:
    """Typed input to the item-across-stages comparison (§T103/§V60).

    ``item_game_id`` identifies the fixed item being compared; ``drops`` are its
    per-stage penguin facts (§V54). The comparison is region-scoped by the service
    (§V5): every stage in ``drops`` is the SAME region as the item.
    """

    server: str
    item_game_id: str
    drops: tuple[ItemStageDrop, ...]


@dataclass(frozen=True)
class ItemFarmingAnalysis:
    """Ranked result of comparing one item across the stages that drop it (§V60/§V66.1).

    ``observation`` is the SINGLE ranked observation (§T129); its ``ranking`` rows are
    ordered ascending by sanity per item (lowest first) -- an ordering + evidence,
    never a "best farm"/mandatory verdict (§V7). The mandatory §V60 comparison caveats
    (stage availability / first-clear / byproduct not modeled) ride the observation's
    observation-level ``limitations`` (they qualify the whole ranking). ``observation``
    is ``None`` when no stage is rankable (the reason rides ``warnings``, §V26); the
    service reports ``not_found`` upstream when there is nothing to compare.
    ``warnings`` records any stage excluded for a missing ``sanity_cost`` or
    ``drop_rate`` (§V26).
    """

    server: str
    item_game_id: str
    observation: RankedObservation | None
    warnings: tuple[str, ...]
    analyzer_version: str = ANALYZER_VERSION


def analyze_item_farming(ctx: ItemFarmingContext) -> ItemFarmingAnalysis:
    """Rank one item's farming efficiency across the stages that drop it (§V60/§V55/§V66.1).

    The §V60 reverse of :func:`analyze_farming`: for a fixed item, each stage's
    sanity-per-item is computed via the shared :func:`_efficiency` core (§V37) and the
    ranking rows are ordered ASCENDING (lowest sanity per copy first) inside a SINGLE
    ranked observation (§T129) -- an ordering + evidence, never a "best farm"/mandatory
    verdict (§V7). Each ranking row is keyed by the stage's ``stage_game_id`` -- the
    UNAMBIGUOUS evidence ref that joins to the sibling stages facts list -- with the
    ``stage_code`` shown alongside as the display ``name`` (§V68/B57: the normal + tough
    variants share a ``stage_code`` like "14-18", so the code alone is undecidable). A
    stage with a missing ``sanity_cost`` or an absent/non-positive
    ``drop_rate`` is excluded with a §V26 warning (never a fabricated figure); the
    per-stage warnings are emitted in stage order so the output is deterministic
    (§V26). An expired stage's figure is downgraded (marked ``expired``, confidence
    reduced via ``_efficiency``, caveat hoisted once, §V85) but KEPT in the ranking,
    not dropped (§V60/§V53). The mandatory §V60 comparison caveats ride the
    observation's observation-level ``limitations`` whenever a ranking exists.
    """
    warnings: list[str] = []
    rows: list[tuple[float, str, str, RankingRow]] = []

    for drop in sorted(ctx.drops, key=lambda d: (d.stage_code or "", d.stage_game_id)):
        # §V68/B57: identify a stage by its UNAMBIGUOUS stage_game_id -- a stage_code
        # like "14-18" is shared by the normal + tough variants -- with the stage_code
        # shown alongside for display, so a warning names one decidable stage.
        stage_ref = (
            f"{drop.stage_game_id} ({drop.stage_code})" if drop.stage_code else drop.stage_game_id
        )
        if drop.sanity_cost is None or drop.sanity_cost <= 0:
            warnings.append(
                f"{stage_ref}: stage sanity cost is missing or non-positive; "
                "sanity-per-item not computed"
            )
            continue
        if drop.drop_rate is None or drop.drop_rate <= 0:
            warnings.append(
                f"{stage_ref}: drop rate is missing or non-positive; sanity-per-item not computed"
            )
            continue
        # §V68/B57: the ranking row's id (the observation evidence ref) is the
        # stage_game_id -- an UNAMBIGUOUS, stable id that joins 1:1 to the sibling
        # stages facts list (which keys on stage_game_id), never the stage_code alone
        # (shared normal/tough). The stage_code rides as the display ``name`` alongside.
        key, row = _ranking_row(
            entity_id=drop.stage_game_id,
            name=drop.stage_code,
            sanity_cost=drop.sanity_cost,
            drop_rate=drop.drop_rate,
            sample_size=drop.sample_size,
            expired=drop.expired,
        )
        # Sort ascending by sanity/item, tie-broken by stage identity so equal-cost
        # stages rank deterministically (§V26/§V60).
        rows.append((key, drop.stage_code or "", drop.stage_game_id, row))

    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    ranking = tuple(row for _, _, _, row in rows)

    # The mandatory comparison caveats accompany an actual ranking; with no rankable
    # stage there is no comparison to qualify (the service reports not_found upstream).
    observation = (
        _ranked_observation(
            summary=_ITEM_SUMMARY,
            ranking=ranking,
            limitations=_ITEM_COMPARISON_LIMITATIONS,
        )
        if ranking
        else None
    )
    return ItemFarmingAnalysis(
        server=ctx.server,
        item_game_id=ctx.item_game_id,
        observation=observation,
        warnings=tuple(warnings),
    )
