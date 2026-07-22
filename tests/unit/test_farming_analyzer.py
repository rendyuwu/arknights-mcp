"""§T90/§T129 farming-efficiency analyzer tests (§V6, §V8, §V26, §V55, §V7, §V66.1).

Drive :func:`~arknights_mcp.analyzers.farming.analyze_farming` /
:func:`~arknights_mcp.analyzers.farming.analyze_item_farming` directly over typed,
DB-free contexts (the ``get_stage_drops`` / ``get_item_drops`` services build these
from the penguin drop cache + the stage's sanity cost). The analyzer is pure, so
these assert the deterministic contract without a database.

Emission shape (§V66.1/§T129): each entry point emits a SINGLE ranked observation.
The five §V6 fields are stated once at the observation level; per-entity data lives
in ``ranking`` rows ``{id, name, sanity_per_item}`` ranked ascending; a row carries
its own ``confidence`` / ``limitations`` ONLY where it deviates (thin sample /
expired). These tests assert:

* one observation with rule_id + confidence + analyzer_version once, and ranking
  rows whose ``id`` REFERENCES the sibling facts (no re-copied numbers, §V66.1/§V6);
* a fresh, well-sampled row is non-deviating (omits its own confidence + limitation)
  and the observation baseline confidence is at/above the §V8 threshold;
* a drop sample below the floor / an unreported sample -> that row deviates below the
  §V8 threshold with a limitation (§V8/§V55);
* an expired cache -> every row is downgraded below the §V8 threshold with a
  limitation (§V53/§V55), both caveats fire together when a row is also thin;
* a missing sanity cost or absent/zero drop rate -> a §V26 warning + no observation,
  never a fabricated or divide-by-zero conclusion;
* the ranking is ascending by sanity per item, tie-broken deterministically (§V60);
* summaries state the computed cost, never a "best farm"/"mandatory" verdict (§V7);
* §V37: the stage view and the item comparison share one figure + confidence core.
"""

from __future__ import annotations

from arknights_mcp.analyzers import ANALYZER_VERSION
from arknights_mcp.analyzers.farming import (
    RULE_ID,
    SAMPLE_SIZE_FLOOR,
    DropFact,
    FarmingContext,
    ItemFarmingContext,
    ItemStageDrop,
    RankedObservation,
    analyze_farming,
    analyze_item_farming,
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


def _obs(analysis) -> RankedObservation:  # type: ignore[no-untyped-def]
    """The single ranked observation the analysis must carry (§V66.1)."""
    assert analysis.observation is not None
    return analysis.observation


def _rows(analysis) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {row.id: row for row in _obs(analysis).ranking}


# --- §V66.1/§V6: ONE ranked observation, fields once, rows reference the facts ---


def test_single_ranked_observation_carries_the_five_fields_once() -> None:
    obs = _obs(analyze_farming(_ctx(_drop())))
    # §V6 identity stated once at the observation level.
    assert obs.rule_id == RULE_ID
    assert obs.category == "farming"
    assert obs.tag == "sanity_per_item"
    assert 0.0 <= obs.confidence <= 1.0
    assert isinstance(obs.limitations, tuple)
    assert obs.analyzer_version == ANALYZER_VERSION
    # §V66.1: the per-entity data lives in ranking rows, not N observations.
    assert len(obs.ranking) == 1
    row = obs.ranking[0]
    # §V66.1: the row's id REFERENCES the sibling drops facts (never a re-copied number).
    assert row.id == "sugar"
    assert row.name == "Sugar"
    assert row.sanity_per_item == 72.0  # 18 / 0.25


def test_ranking_row_omits_reinstated_numbers() -> None:
    # §V66.1: a row carries only {id, name, sanity_per_item} (+ deviation fields); the
    # sanity_cost / drop_rate / sample_size are NOT re-copied -- they live in the
    # sibling drops list the client joins on via ``id``.
    row = _obs(analyze_farming(_ctx(_drop()))).ranking[0]
    for reinstated in ("sanity_cost", "drop_rate", "sample_size", "times", "quantity"):
        assert not hasattr(row, reinstated)


def test_sanity_per_item_computed_from_typed_fields() -> None:
    # 18 sanity / 0.25 drops-per-run = 72 sanity per item, from the two typed fields.
    row = _obs(analyze_farming(_ctx(_drop(drop_rate=0.25), sanity_cost=18))).ranking[0]
    assert row.sanity_per_item == 72.0


# --- §V8/§V55: a non-deviating row omits its own confidence/limitation ----------


def test_sufficient_sample_row_is_non_deviating() -> None:
    obs = _obs(analyze_farming(_ctx(_drop(sample_size=SAMPLE_SIZE_FLOOR))))
    # §V8: fresh + well-sampled -> the observation baseline is a recommendation-grade
    # confidence and the row inherits it (its own confidence/limitations are omitted).
    assert obs.confidence >= 0.5
    row = obs.ranking[0]
    assert row.confidence is None
    assert row.limitations == ()


# --- §V8/§V55: a thin / unreported sample -> that row deviates below the floor ---


def test_thin_sample_row_deviates_with_limitation() -> None:
    row = _obs(analyze_farming(_ctx(_drop(sample_size=SAMPLE_SIZE_FLOOR - 1)))).ranking[0]
    # §V8: below the floor the figure is not a recommendation -- row confidence < 0.5.
    assert row.confidence is not None and row.confidence < 0.5
    assert any("below the" in lim and "floor" in lim for lim in row.limitations)


def test_missing_sample_size_row_is_unverified_not_zero() -> None:
    # §V26: an absent sample size is not treated as a stable rate -- the row deviates
    # with reduced confidence + a limitation, never silently accepted as well-sampled.
    row = _obs(analyze_farming(_ctx(_drop(sample_size=None)))).ranking[0]
    assert row.confidence is not None and row.confidence < 0.5
    assert any("sample size not reported" in lim for lim in row.limitations)


# --- §V53/§V55: expired cache -> the row is downgraded, not fresh ---------------


def test_expired_cache_row_downgraded_to_limitation() -> None:
    row = _obs(analyze_farming(_ctx(_drop(), expired=True))).ranking[0]
    # §V55: an expired figure is never a fresh recommendation.
    assert row.confidence is not None and row.confidence < 0.5
    assert any("expired" in lim for lim in row.limitations)


def test_expired_and_thin_sample_row_carries_both_limitations() -> None:
    # §V6/§V55: when a drop is BOTH expired and below the sample floor, neither cause
    # masks the other -- the row records both caveats (stale AND noisy), not just one.
    row = _obs(
        analyze_farming(_ctx(_drop(sample_size=SAMPLE_SIZE_FLOOR - 1), expired=True))
    ).ranking[0]
    assert row.confidence is not None and row.confidence < 0.5
    assert any("expired" in lim for lim in row.limitations)
    assert any("below the" in lim and "floor" in lim for lim in row.limitations)


# --- §V26: missing inputs -> warning + no observation, never a fabrication -------


def test_missing_sanity_cost_warns_and_makes_no_observation() -> None:
    analysis = analyze_farming(_ctx(_drop(), sanity_cost=None))
    assert analysis.observation is None
    assert any("sanity cost" in w for w in analysis.warnings)


def test_absent_or_zero_drop_rate_warns_per_item_no_divide_by_zero() -> None:
    analysis = analyze_farming(_ctx(_drop("a", drop_rate=None), _drop("b", drop_rate=0.0)))
    assert analysis.observation is None
    assert any("a: drop rate" in w for w in analysis.warnings)
    assert any("b: drop rate" in w for w in analysis.warnings)


# --- §V7: conservative, no prescriptive language -------------------------------


def test_no_prescriptive_language() -> None:
    obs = _obs(analyze_farming(_ctx(_drop())))
    blob = f"{obs.title} {obs.summary} {' '.join(obs.limitations)}".lower()
    assert not any(word in blob for word in _PRESCRIPTIVE)


# --- §V60/§V66.1: the ranking is ascending by sanity per item -------------------


def test_stage_ranking_ascending_by_sanity_per_item() -> None:
    # Fixed sanity_cost 18: rate 0.5 -> 36, rate 0.25 -> 72, rate 0.1 -> 180. Seeded out
    # of order -> the ranking rows must reorder ascending (lowest sanity per copy first).
    obs = _obs(
        analyze_farming(
            _ctx(
                _drop("hi", drop_rate=0.1),
                _drop("lo", drop_rate=0.5),
                _drop("mid", drop_rate=0.25),
            )
        )
    )
    ids = [row.id for row in obs.ranking]
    figures = [row.sanity_per_item for row in obs.ranking]
    assert ids == ["lo", "mid", "hi"]
    assert figures == [36.0, 72.0, 180.0]
    assert figures == sorted(figures)


def test_stage_ranking_ties_broken_by_item_id() -> None:
    # Equal sanity per item -> tie-broken by item_game_id, deterministically (§V26).
    obs = _obs(analyze_farming(_ctx(_drop("zzz"), _drop("aaa"), _drop("mmm"))))
    assert [row.id for row in obs.ranking] == ["aaa", "mmm", "zzz"]


# --- §T103/§V60: item -> stage comparison (reverse of the stage view) ----------


def _stage_drop(
    stage_code: str,
    *,
    stage_game_id: str | None = None,
    sanity_cost: int | None = 18,
    drop_rate: float | None = 0.25,
    sample_size: int | None = 5000,
    expired: bool = False,
) -> ItemStageDrop:
    return ItemStageDrop(
        stage_code=stage_code,
        stage_game_id=stage_game_id or f"level_{stage_code}",
        sanity_cost=sanity_cost,
        drop_rate=drop_rate,
        sample_size=sample_size,
        expired=expired,
    )


def _item_ctx(*drops: ItemStageDrop, item_game_id: str = "sugar") -> ItemFarmingContext:
    return ItemFarmingContext(server="en", item_game_id=item_game_id, drops=tuple(drops))


def _item_obs(analysis) -> RankedObservation:  # type: ignore[no-untyped-def]
    assert analysis.observation is not None
    return analysis.observation


def test_item_comparison_ranked_ascending_by_sanity_per_item() -> None:
    # §V60: lowest sanity-per-item first. 4-4 costs 18/0.25=72; a-1 costs 6/0.5=12;
    # b-2 costs 30/0.25=120. Seeded out of order -> ranking must reorder to 12, 72, 120.
    obs = _item_obs(
        analyze_item_farming(
            _item_ctx(
                _stage_drop("4-4", sanity_cost=18, drop_rate=0.25),
                _stage_drop("b-2", sanity_cost=30, drop_rate=0.25),
                _stage_drop("a-1", sanity_cost=6, drop_rate=0.5),
            )
        )
    )
    ids = [row.id for row in obs.ranking]
    figures = [row.sanity_per_item for row in obs.ranking]
    assert ids == ["a-1", "4-4", "b-2"]
    assert figures == [12.0, 72.0, 120.0]
    assert figures == sorted(figures)


def test_item_comparison_carries_mandatory_limitations_on_observation() -> None:
    # §V60/§V66.1: the mandatory availability / first-clear / byproduct caveats ride the
    # single observation's observation-level limitations (stated once, not per row).
    obs = _item_obs(analyze_item_farming(_item_ctx(_stage_drop("4-4"), _stage_drop("a-1"))))
    blob = " ".join(obs.limitations).lower()
    assert "availability" in blob
    assert "first-clear" in blob or "first clear" in blob
    assert "byproduct" in blob or "synthesis" in blob


def test_item_comparison_no_ranking_no_observation() -> None:
    # With no rankable stage there is no comparison to qualify: no observation, and the
    # mandatory caveats do not appear (the service reports not_found upstream).
    analysis = analyze_item_farming(_item_ctx(_stage_drop("4-4", drop_rate=None)))
    assert analysis.observation is None
    assert any("4-4: drop rate" in w for w in analysis.warnings)


def test_item_comparison_expired_stage_kept_not_dropped() -> None:
    # §V60/§V53: an expired stage's figure is downgraded (its row deviates) but stays IN
    # the ranking -- never dropped from the comparison.
    obs = _item_obs(
        analyze_item_farming(
            _item_ctx(
                _stage_drop("4-4", expired=False),
                _stage_drop("a-1", sanity_cost=6, drop_rate=0.5, expired=True),
            )
        )
    )
    rows = {row.id: row for row in obs.ranking}
    assert set(rows) == {"4-4", "a-1"}  # the expired stage is still present
    expired_row = rows["a-1"]
    assert expired_row.confidence is not None and expired_row.confidence < 0.5
    assert any("expired" in lim for lim in expired_row.limitations)
    # the fresh stage is non-deviating (inherits the baseline)
    assert rows["4-4"].confidence is None and rows["4-4"].limitations == ()


def test_item_comparison_excludes_missing_inputs_with_warning() -> None:
    # §V26: a stage with a missing sanity_cost or absent drop rate is excluded with a
    # warning, never a fabricated figure or a divide-by-zero.
    analysis = analyze_item_farming(
        _item_ctx(
            _stage_drop("ok-1"),
            _stage_drop("no-sanity", sanity_cost=None),
            _stage_drop("no-rate", drop_rate=0.0),
        )
    )
    ids = [row.id for row in _item_obs(analysis).ranking]
    assert ids == ["ok-1"]
    assert any("no-sanity: stage sanity cost" in w for w in analysis.warnings)
    assert any("no-rate: drop rate" in w for w in analysis.warnings)


def test_item_comparison_observation_carries_five_fields() -> None:
    # §V6: the ranked observation is fully attributed; the row id = the STAGE ref.
    analysis = analyze_item_farming(_item_ctx(_stage_drop("4-4")))
    obs = _item_obs(analysis)
    assert obs.rule_id == RULE_ID
    assert obs.category == "farming"
    assert 0.0 <= obs.confidence <= 1.0
    assert obs.analyzer_version == ANALYZER_VERSION
    assert [row.id for row in obs.ranking] == ["4-4"]
    assert analysis.analyzer_version == ANALYZER_VERSION


def test_item_comparison_no_prescriptive_language() -> None:
    # §V7/§V55: an ordering + evidence, never a "best farm"/mandatory verdict.
    obs = _item_obs(analyze_item_farming(_item_ctx(_stage_drop("4-4"), _stage_drop("a-1"))))
    blob = f"{obs.title} {obs.summary} {' '.join(obs.limitations)}".lower()
    assert not any(word in blob for word in _PRESCRIPTIVE)


def test_item_comparison_ties_broken_deterministically() -> None:
    # §V26/§V60: equal-cost stages rank by stage_code then game_id, deterministically.
    obs = _item_obs(
        analyze_item_farming(
            _item_ctx(
                _stage_drop("z-9", sanity_cost=18, drop_rate=0.25),
                _stage_drop("a-1", sanity_cost=18, drop_rate=0.25),
                _stage_drop("m-5", sanity_cost=18, drop_rate=0.25),
            )
        )
    )
    assert [row.id for row in obs.ranking] == ["a-1", "m-5", "z-9"]


# --- §V37: the stage view and the item comparison share ONE math + confidence core -


def test_v37_stage_and_item_views_agree_on_figure_and_confidence() -> None:
    # §V37: analyze_farming (stage view) and analyze_item_farming (item view) compute
    # the sanity-per-item figure and the §V8/§V55 confidence ladder in exactly one
    # place, so for the same typed inputs the two views' ranking rows MUST agree on the
    # figure, the (deviating-or-not) confidence, and the limitations -- no second home.
    for sanity, rate, sample, expired in [
        (18, 0.25, 5000, False),
        (30, 0.5, SAMPLE_SIZE_FLOOR - 1, False),  # thin sample
        (12, 0.1, 5000, True),  # expired
        (12, 0.1, None, True),  # expired + unreported sample
    ]:
        stage_row = _obs(
            analyze_farming(
                _ctx(_drop(drop_rate=rate, sample_size=sample), sanity_cost=sanity, expired=expired)
            )
        ).ranking[0]
        drop = _stage_drop(
            "4-4", sanity_cost=sanity, drop_rate=rate, sample_size=sample, expired=expired
        )
        item_row = _item_obs(analyze_item_farming(_item_ctx(drop))).ranking[0]
        assert stage_row.sanity_per_item == item_row.sanity_per_item
        assert stage_row.confidence == item_row.confidence
        # the same conservatism limitations (expiry / thin / unreported) fire in both
        assert set(stage_row.limitations) == set(item_row.limitations)
