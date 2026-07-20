"""Farming-efficiency analyzer (§T90; §V6, §V8, §V26, §V55, §V7).

Deterministic, evidence-backed observations about the sanity cost of farming an
item at a stage. Pure and DB-free: the ``get_stage_drops`` service (§T91) reads the
penguin drop-rate cache (§V53) plus the stage's ``sanity_cost`` into the typed
inputs below and calls :func:`analyze_farming`; there is no natural-language input
-- every figure is computed from typed numeric fields only (§V26/§V55), never from a
name or description string.

The core figure is *sanity per item* = the stage's ``sanity_cost`` divided by the
item's ``drop_rate`` (expected drops per run): the average sanity spent to obtain
one of the item. Each observation carries the five §V6 fields (``rule_id`` +
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


def _drop_observation(drop: DropFact, *, sanity_cost: int, expired: bool) -> Observation:
    """Observation of the sanity-per-item cost for one drop (§V6, §V26, §V55).

    Caller guarantees a positive ``sanity_cost`` and a positive ``drop_rate`` (a
    missing/zero either is a §V26 warning handled upstream), so the ratio is always
    well-defined here.
    """
    assert drop.drop_rate is not None and drop.drop_rate > 0  # guarded by caller
    sanity_per_item = sanity_cost / drop.drop_rate

    limitations: list[str] = []
    # Each conservatism condition INDEPENDENTLY lowers confidence (lowest wins) and
    # records its own limitation, so a drop that is both expired AND thin-sampled
    # carries BOTH caveats -- §V6 wants complete limitations, not a dominant-cause-only
    # note (§V8/§V53/§V55). A fresh, well-sampled rate keeps the stable confidence.
    confidence = _CONF_STABLE
    if expired:
        confidence = min(confidence, _CONF_EXPIRED)
        limitations.append(
            "drop cache expired; farming efficiency downgraded to a limitation, "
            "not a fresh recommendation -- re-sync the penguin drop source"
        )
    if drop.sample_size is not None and drop.sample_size < SAMPLE_SIZE_FLOOR:
        confidence = min(confidence, _CONF_THIN_SAMPLE)
        limitations.append(
            f"drop sample of {drop.sample_size} run(s) is below the "
            f"{SAMPLE_SIZE_FLOOR}-run floor; the rate is noisy and the figure uncertain"
        )

    if drop.sample_size is None:
        # A rate with no reported sample size is unverifiable for stability (§V26).
        confidence = min(confidence, _CONF_THIN_SAMPLE)
        limitations.append("drop sample size not reported; rate stability unverified")

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
            value=round(sanity_per_item, 2),
            note="computed sanity_cost / drop_rate",
        ),
    )
    return Observation(
        rule_id=RULE_ID,
        category=_CATEGORY,
        tag="sanity_per_item",
        title="Farming sanity cost per item",
        summary=(
            f"{_item_label(drop)} costs about {sanity_per_item:.1f} sanity per copy on "
            "this stage (stage sanity cost divided by the expected drops per run)."
        ),
        confidence=confidence,
        evidence=evidence,
        limitations=tuple(limitations),
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
