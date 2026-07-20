"""§T90 farming-efficiency analyzer tests (§V6, §V8, §V26, §V55, §V7).

Drive :func:`~arknights_mcp.analyzers.farming.analyze_farming` directly over typed,
DB-free contexts (the ``get_stage_drops`` service, §T91, builds these from the
penguin drop cache + the stage's sanity cost). The analyzer is pure, so these assert
the deterministic contract without a database:

* every observation carries the five §V6 fields, with typed-field evidence only
  (sanity_cost + drop_rate + sample_size + the computed figure; §V26/§V55);
* a drop sample below the floor -> reduced confidence + a limitation (§V8/§V55);
* an expired drop cache -> the figure is downgraded to a limitation, confidence held
  under the §V8 recommendation threshold, never a fresh recommendation (§V55);
* a missing sanity cost or absent/zero drop rate -> a §V26 warning, never a
  fabricated or divide-by-zero conclusion;
* summaries state the computed cost, never a "best farm"/"mandatory" verdict (§V7);
* drops are processed in a stable order (deterministic, §V26).
"""

from __future__ import annotations

from arknights_mcp.analyzers import ANALYZER_VERSION
from arknights_mcp.analyzers.farming import (
    RULE_ID,
    SAMPLE_SIZE_FLOOR,
    DropFact,
    FarmingContext,
    analyze_farming,
)

_PRESCRIPTIVE = ("best farm", "best-farm", "mandatory", "must ", "should ", "always farm")


def _drop(
    item_game_id: str = "sugar",
    *,
    display_name: str | None = "Sugar",
    drop_rate: float | None = 0.25,
    sample_size: int | None = 5000,
) -> DropFact:
    return DropFact(
        item_game_id=item_game_id,
        item_display_name=display_name,
        drop_rate=drop_rate,
        sample_size=sample_size,
    )


def _ctx(
    *drops: DropFact,
    sanity_cost: int | None = 18,
    expired: bool = False,
) -> FarmingContext:
    return FarmingContext(
        server="en",
        stage_code="4-4",
        sanity_cost=sanity_cost,
        drops=tuple(drops),
        expired=expired,
    )


def _only(analysis) -> object:  # type: ignore[no-untyped-def]
    assert len(analysis.observations) == 1
    return analysis.observations[0]


# --- §V6: every observation is fully attributed --------------------------------


def test_observation_carries_the_five_fields() -> None:
    obs = _only(analyze_farming(_ctx(_drop())))
    # §V6: rule_id + evidence + confidence + limitations + analyzer_version.
    assert obs.rule_id == RULE_ID
    assert obs.category == "farming"
    assert obs.evidence  # never a bare verdict
    assert 0.0 <= obs.confidence <= 1.0
    assert isinstance(obs.limitations, tuple)
    assert obs.analyzer_version == ANALYZER_VERSION
    for ev in obs.evidence:
        assert ev.ref == "sugar" and ev.field


# --- §V26/§V55: computed from typed fields, evidence carries the inputs ---------


def test_sanity_per_item_computed_from_typed_fields() -> None:
    # 18 sanity / 0.25 drops-per-run = 72 sanity per item, from the two typed fields.
    obs = _only(analyze_farming(_ctx(_drop(drop_rate=0.25), sanity_cost=18)))
    by_field = {ev.field: ev.value for ev in obs.evidence}
    assert by_field["sanity_cost"] == 18
    assert by_field["drop_rate"] == 0.25
    assert by_field["sample_size"] == 5000
    assert by_field["sanity_per_item"] == 72.0
    assert "72" in obs.summary  # stated factually in the headline


# --- §V8/§V55: thin sample -> reduced confidence + limitation -------------------


def test_thin_sample_reduces_confidence_and_records_limitation() -> None:
    thin = _drop(sample_size=SAMPLE_SIZE_FLOOR - 1)
    obs = _only(analyze_farming(_ctx(thin)))
    # §V8: below the floor the figure is not a recommendation -- confidence < 0.5.
    assert obs.confidence < 0.5
    assert any("below the" in lim and "floor" in lim for lim in obs.limitations)


def test_sufficient_sample_keeps_stable_confidence() -> None:
    obs = _only(analyze_farming(_ctx(_drop(sample_size=SAMPLE_SIZE_FLOOR))))
    assert obs.confidence >= 0.5
    assert obs.limitations == ()


def test_missing_sample_size_is_unverified_not_zero() -> None:
    # §V26: an absent sample size is not treated as a stable rate -- reduced
    # confidence + a limitation, never silently accepted as well-sampled.
    obs = _only(analyze_farming(_ctx(_drop(sample_size=None))))
    assert obs.confidence < 0.5
    assert any("sample size not reported" in lim for lim in obs.limitations)


# --- §V53/§V55: expired cache -> downgraded to a limitation, not fresh ----------


def test_expired_cache_downgrades_to_limitation() -> None:
    obs = _only(analyze_farming(_ctx(_drop(), expired=True)))
    # §V55: an expired figure is never a fresh recommendation.
    assert obs.confidence < 0.5
    assert any("expired" in lim for lim in obs.limitations)


def test_expired_and_thin_sample_carries_both_limitations() -> None:
    # §V6/§V55: when a drop is BOTH expired and below the sample floor, neither cause
    # masks the other -- both caveats are recorded (a client sees the figure is stale
    # AND noisy), not just the expiry note.
    thin = _drop(sample_size=SAMPLE_SIZE_FLOOR - 1)
    obs = _only(analyze_farming(_ctx(thin, expired=True)))
    assert obs.confidence < 0.5
    assert any("expired" in lim for lim in obs.limitations)
    assert any("below the" in lim and "floor" in lim for lim in obs.limitations)


# --- §V26: missing inputs -> warning, never a fabricated conclusion -------------


def test_missing_sanity_cost_warns_and_computes_nothing() -> None:
    analysis = analyze_farming(_ctx(_drop(), sanity_cost=None))
    assert analysis.observations == ()
    assert any("sanity cost" in w for w in analysis.warnings)


def test_absent_or_zero_drop_rate_warns_per_item_no_divide_by_zero() -> None:
    analysis = analyze_farming(_ctx(_drop("a", drop_rate=None), _drop("b", drop_rate=0.0)))
    assert analysis.observations == ()
    assert any("a: drop rate" in w for w in analysis.warnings)
    assert any("b: drop rate" in w for w in analysis.warnings)


# --- §V7: conservative, no prescriptive language -------------------------------


def test_no_prescriptive_language() -> None:
    obs = _only(analyze_farming(_ctx(_drop())))
    blob = f"{obs.title} {obs.summary}".lower()
    assert not any(word in blob for word in _PRESCRIPTIVE)


# --- deterministic order -------------------------------------------------------


def test_drops_processed_in_item_id_order() -> None:
    analysis = analyze_farming(_ctx(_drop("zzz"), _drop("aaa"), _drop("mmm")))
    refs = [obs.evidence[0].ref for obs in analysis.observations]
    assert refs == ["aaa", "mmm", "zzz"]
