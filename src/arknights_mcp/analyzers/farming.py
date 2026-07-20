"""Farming-efficiency analyzer (§T90/§T103; §V6, §V8, §V26, §V55, §V7, §V60).

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
confidence ladder. Each observation carries the five §V6 fields (``rule_id`` +
evidence + confidence + limitations + ``analyzer_version``) reusing the shared
:class:`~arknights_mcp.analyzers.base.Observation` / ``EvidenceItem`` vocabulary
(§V37). Observations state the computed cost factually -- never a
"best farm" / "mandatory" verdict (§V7).

Conservatism (§V8/§V55):

* a drop sample below :data:`SAMPLE_SIZE_FLOOR` runs -> confidence is reduced and a
  limitation records the thin sample (the rate is noisy, §V55 extends §V8);
* an expired drop cache (§V53) -> the efficiency is downgraded to a limitation and
  confidence is forced below the §V8 recommendation threshold, never presented as a
  fresh recommendation (§V55);
* a missing ``sanity_cost`` or an absent/zero ``drop_rate`` yields no figure -- it is
  a §V26 warning, never a fabricated or divide-by-zero conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass

from arknights_mcp.analyzers.base import ANALYZER_VERSION, EvidenceItem, Observation

RULE_ID = "farming.sanity_per_item"
_CATEGORY = "farming"

#: A drop sample of at least this many runs is treated as a stable rate; below it the
#: rate is noisy, so confidence is reduced and a limitation is recorded (§V8/§V55).
SAMPLE_SIZE_FLOOR = 100

#: Confidence when the drop rate rests on a sufficient sample and the cache is fresh:
#: the figure is a deterministic ratio of two typed numeric fields (§V26), so it is
#: high but never certain (sampling variance remains).
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
    """Aggregate result of running the farming rule over one stage's drops (§V6)."""

    server: str
    stage_code: str | None
    observations: tuple[Observation, ...]
    warnings: tuple[str, ...]
    analyzer_version: str = ANALYZER_VERSION


def _item_label(drop: DropFact) -> str:
    """A stable handle for an item in a summary (the allowlisted name or its id).

    The ``display_name`` is a proper name, not prose; the ``game_id`` keeps summaries
    deterministic and language-neutral when the name is absent (§V26).
    """
    return drop.item_display_name or drop.item_game_id


@dataclass(frozen=True)
class _Efficiency:
    """The computed sanity-per-item figure plus its §V8/§V55 confidence + limitations.

    The single §V37 home's output: the farming math and the conservatism ladder are
    computed once (:func:`_efficiency`) so the stage view (:func:`analyze_farming`) and
    the item comparison (:func:`analyze_item_farming`) can never diverge on the figure
    or on how a thin/absent sample or an expired cache lowers confidence.
    """

    sanity_per_item: float
    confidence: float
    limitations: tuple[str, ...]


def _efficiency(
    *, sanity_cost: int, drop_rate: float, sample_size: int | None, expired: bool
) -> _Efficiency:
    """Compute sanity per item + the §V8/§V55 confidence/limitations (§V37 single home).

    Caller guarantees a positive ``sanity_cost`` and a positive ``drop_rate`` (a
    missing/zero either is a §V26 warning handled upstream), so the ratio is always
    well-defined. Each conservatism condition INDEPENDENTLY lowers confidence (lowest
    wins) and appends its own limitation, so a figure that is both expired AND
    thin-sampled carries BOTH caveats -- §V6 wants complete limitations, not a
    dominant-cause-only note (§V8/§V53/§V55). A fresh, well-sampled rate keeps the
    stable confidence.
    """
    sanity_per_item = sanity_cost / drop_rate

    limitations: list[str] = []
    confidence = _CONF_STABLE
    if expired:
        confidence = min(confidence, _CONF_EXPIRED)
        limitations.append(
            "drop cache expired; farming efficiency downgraded to a limitation, "
            "not a fresh recommendation -- re-sync the penguin drop source"
        )
    if sample_size is not None and sample_size < SAMPLE_SIZE_FLOOR:
        confidence = min(confidence, _CONF_THIN_SAMPLE)
        limitations.append(
            f"drop sample of {sample_size} run(s) is below the "
            f"{SAMPLE_SIZE_FLOOR}-run floor; the rate is noisy and the figure uncertain"
        )
    if sample_size is None:
        # A rate with no reported sample size is unverifiable for stability (§V26).
        confidence = min(confidence, _CONF_THIN_SAMPLE)
        limitations.append("drop sample size not reported; rate stability unverified")

    return _Efficiency(
        sanity_per_item=sanity_per_item,
        confidence=confidence,
        limitations=tuple(limitations),
    )


def _drop_observation(drop: DropFact, *, sanity_cost: int, expired: bool) -> Observation:
    """Observation of the sanity-per-item cost for one drop (§V6, §V26, §V55).

    Caller guarantees a positive ``sanity_cost`` and a positive ``drop_rate`` (a
    missing/zero either is a §V26 warning handled upstream), so the ratio is always
    well-defined here. The figure + confidence + limitations come from the shared
    :func:`_efficiency` core (§V37); this shaper only builds the item-ref evidence and
    the factual summary on top.
    """
    assert drop.drop_rate is not None and drop.drop_rate > 0  # guarded by caller
    eff = _efficiency(
        sanity_cost=sanity_cost,
        drop_rate=drop.drop_rate,
        sample_size=drop.sample_size,
        expired=expired,
    )

    evidence = (
        EvidenceItem(
            ref=drop.item_game_id,
            field="sanity_cost",
            value=sanity_cost,
            note="stage sanity cost per run",
        ),
        EvidenceItem(
            ref=drop.item_game_id,
            field="drop_rate",
            value=drop.drop_rate,
            note="expected drops per run (penguin quantity/times)",
        ),
        EvidenceItem(
            ref=drop.item_game_id,
            field="sample_size",
            value=drop.sample_size,
            note="penguin sampled runs (times)",
        ),
        EvidenceItem(
            ref=drop.item_game_id,
            field="sanity_per_item",
            value=round(eff.sanity_per_item, 2),
            note="computed sanity_cost / drop_rate",
        ),
    )
    return Observation(
        rule_id=RULE_ID,
        category=_CATEGORY,
        tag="sanity_per_item",
        title="Farming sanity cost per item",
        summary=(
            f"{_item_label(drop)} costs about {eff.sanity_per_item:.1f} sanity per copy on "
            "this stage (stage sanity cost divided by the expected drops per run)."
        ),
        confidence=eff.confidence,
        evidence=evidence,
        limitations=eff.limitations,
    )


def analyze_farming(ctx: FarmingContext) -> FarmingAnalysis:
    """Run the deterministic farming-efficiency rule over ``ctx`` (§V6, §V26, §V55).

    Drops are processed in ``item_game_id`` order so observations + warnings are
    emitted deterministically (§V26). A stage with no ``sanity_cost`` warns once and
    computes nothing (§V26); a drop with an absent or non-positive ``drop_rate`` warns
    per item, never fabricating a figure or dividing by zero. Every emitted
    observation carries the five §V6 fields; the analyzer adds no prescriptive
    language (§V7).
    """
    observations: list[Observation] = []
    warnings: list[str] = []

    if ctx.sanity_cost is None or ctx.sanity_cost <= 0:
        warnings.append(
            "stage sanity cost is missing or non-positive; farming efficiency not computed"
        )
        return FarmingAnalysis(
            server=ctx.server,
            stage_code=ctx.stage_code,
            observations=(),
            warnings=tuple(warnings),
        )

    for drop in sorted(ctx.drops, key=lambda d: d.item_game_id):
        if drop.drop_rate is None or drop.drop_rate <= 0:
            warnings.append(
                f"{drop.item_game_id}: drop rate is missing or non-positive; "
                "sanity-per-item not computed"
            )
            continue
        observations.append(
            _drop_observation(drop, sanity_cost=ctx.sanity_cost, expired=ctx.expired)
        )

    return FarmingAnalysis(
        server=ctx.server,
        stage_code=ctx.stage_code,
        observations=tuple(observations),
        warnings=tuple(warnings),
    )


# --- §T103/§V60: item -> stage comparison (reverse of the stage view) ----------

#: The §V60 comparison-wide caveats every item->stage ranking MUST carry: the drop
#: cache models neither a stage's open/closed availability (an event stage is not
#: permanently farmable) nor the one-time first-clear bonus, and byproduct /
#: synthesis-recipe routes to the item are not modeled. The ranking is
#: sanity-per-direct-drop only; the decision stays the user's (§V7).
_ITEM_COMPARISON_LIMITATIONS: tuple[str, ...] = (
    "stage availability is not modeled: an event stage may be closed and not currently "
    "farmable; the drop cache does not track open/closed windows",
    "the one-time first-clear bonus is not modeled; only the repeatable drop rate is",
    "byproduct and synthesis-recipe routes to this item are not modeled; only direct "
    "stage drops are ranked",
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
    """Ranked result of comparing one item across the stages that drop it (§V60).

    ``observations`` are ranked ascending by sanity per item (lowest first) -- an
    ordering + evidence, never a "best farm"/mandatory verdict (§V7). ``limitations``
    carries the mandatory §V60 comparison caveats (stage availability / first-clear /
    byproduct not modeled); ``warnings`` records any stage excluded for a missing
    ``sanity_cost`` or ``drop_rate`` (§V26). Every observation still carries its own
    five §V6 fields (incl. the expired/thin-sample limitation from :func:`_efficiency`).
    """

    server: str
    item_game_id: str
    observations: tuple[Observation, ...]
    limitations: tuple[str, ...]
    warnings: tuple[str, ...]
    analyzer_version: str = ANALYZER_VERSION


def _item_stage_observation(drop: ItemStageDrop) -> tuple[float, Observation]:
    """Efficiency observation for one stage's drop of the item, plus its sort key (§V6).

    Caller guarantees a positive ``sanity_cost`` and ``drop_rate``. Reuses the shared
    :func:`_efficiency` core (§V37); the evidence ``ref`` is the STAGE (the comparison
    varies stages for a fixed item, the reverse of the stage view), and the returned
    float is the sanity-per-item used to rank ascending (§V60).
    """
    assert drop.sanity_cost is not None and drop.sanity_cost > 0  # guarded by caller
    assert drop.drop_rate is not None and drop.drop_rate > 0  # guarded by caller
    eff = _efficiency(
        sanity_cost=drop.sanity_cost,
        drop_rate=drop.drop_rate,
        sample_size=drop.sample_size,
        expired=drop.expired,
    )
    stage_ref = drop.stage_code or drop.stage_game_id
    evidence = (
        EvidenceItem(
            ref=stage_ref,
            field="sanity_cost",
            value=drop.sanity_cost,
            note="stage sanity cost per run",
        ),
        EvidenceItem(
            ref=stage_ref,
            field="drop_rate",
            value=drop.drop_rate,
            note="expected drops per run (penguin quantity/times)",
        ),
        EvidenceItem(
            ref=stage_ref,
            field="sample_size",
            value=drop.sample_size,
            note="penguin sampled runs (times)",
        ),
        EvidenceItem(
            ref=stage_ref,
            field="sanity_per_item",
            value=round(eff.sanity_per_item, 2),
            note="computed sanity_cost / drop_rate",
        ),
    )
    obs = Observation(
        rule_id=RULE_ID,
        category=_CATEGORY,
        tag="sanity_per_item",
        title="Farming sanity cost per item",
        summary=(
            f"{stage_ref} yields this item for about {eff.sanity_per_item:.1f} sanity per "
            "copy (stage sanity cost divided by the expected drops per run)."
        ),
        confidence=eff.confidence,
        evidence=evidence,
        limitations=eff.limitations,
    )
    return eff.sanity_per_item, obs


def analyze_item_farming(ctx: ItemFarmingContext) -> ItemFarmingAnalysis:
    """Rank one item's farming efficiency across the stages that drop it (§V60/§V55).

    The §V60 reverse of :func:`analyze_farming`: for a fixed item, each stage's
    sanity-per-item is computed via the shared :func:`_efficiency` core (§V37) and the
    observations are RANKED ASCENDING (lowest sanity per copy first) -- an ordering +
    evidence, never a "best farm"/mandatory verdict (§V7). A stage with a missing
    ``sanity_cost`` or an absent/non-positive ``drop_rate`` is excluded with a §V26
    warning (never a fabricated figure); the per-stage warnings are emitted in stage
    order so the output is deterministic (§V26). An expired stage's figure is
    downgraded to a limitation (via ``_efficiency``) but KEPT in the ranking, not
    dropped (§V60/§V53). The mandatory §V60 comparison caveats ride the analysis
    ``limitations`` whenever a ranking exists.
    """
    ranked: list[tuple[float, str, str, Observation]] = []
    warnings: list[str] = []

    for drop in sorted(ctx.drops, key=lambda d: (d.stage_code or "", d.stage_game_id)):
        stage_ref = drop.stage_code or drop.stage_game_id
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
        sanity_per_item, obs = _item_stage_observation(drop)
        # Sort ascending by sanity/item, tie-broken by stage identity so equal-cost
        # stages rank deterministically (§V26/§V60).
        ranked.append((sanity_per_item, drop.stage_code or "", drop.stage_game_id, obs))

    ranked.sort(key=lambda r: (r[0], r[1], r[2]))
    observations = tuple(obs for _, _, _, obs in ranked)

    return ItemFarmingAnalysis(
        server=ctx.server,
        item_game_id=ctx.item_game_id,
        observations=observations,
        # The mandatory comparison caveats accompany an actual ranking; with no rankable
        # stage there is no comparison to qualify (the service reports not_found upstream).
        limitations=_ITEM_COMPARISON_LIMITATIONS if observations else (),
        warnings=tuple(warnings),
    )
