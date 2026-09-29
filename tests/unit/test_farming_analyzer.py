"""Farming-efficiency analyzer tests.

Drive :func:`~arknights_mcp.analyzers.farming.analyze_farming` /
:func:`~arknights_mcp.analyzers.farming.analyze_item_farming` directly over typed,
DB-free contexts (the ``get_stage_drops`` / ``get_item_drops`` services build these
from the penguin drop cache + the stage's sanity cost). The analyzer is pure, so
these assert the deterministic contract without a database.

Emission shape: each entry point emits a SINGLE ranked observation.
The five fields are stated once at the observation level; per-entity data lives
in ``ranking`` rows ``{id, name, sanity_per_item}`` ranked ascending; a row carries
its own ``confidence`` + a typed ``expired``/``flags`` marker ONLY where it deviates
(thin sample / expired), and the sentence explaining each condition is hoisted ONCE
onto the observation-level limitations. These tests assert:

* one observation with rule_id + confidence + analyzer_version once, and ranking
  rows whose ``id`` REFERENCES the sibling facts (no re-copied numbers);
* a fresh, well-sampled row is non-deviating (omits its own confidence + markers)
  and the observation baseline confidence is at/above the threshold;
* a drop sample below the floor / an unreported sample -> that row deviates below the
  threshold with a typed flag, the sentence hoisted once;
* an expired cache -> every row is downgraded below the threshold and marked
  expired, both caveats fire together when a row is also thin;
* N deviating rows -> each hoisted sentence appears exactly once, never per row;
* a missing sanity cost or absent/zero drop rate -> a warning + no observation,
  never a fabricated or divide-by-zero conclusion;
* the ranking is ascending by sanity per item, tie-broken deterministically;
* summaries state the computed cost, never a "best farm"/"mandatory" verdict;
* the stage view and the item comparison share one figure + confidence core.
"""

from __future__ import annotations

from arknights_mcp.analyzers import ANALYZER_VERSION
from arknights_mcp.analyzers.farming import (
    FLAG_SAMPLE_UNREPORTED,
    FLAG_THIN_SAMPLE,
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
    """The single ranked observation the analysis must carry."""
    assert analysis.observation is not None
    return analysis.observation


def _rows(analysis) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {row.id: row for row in _obs(analysis).ranking}


# --- ONE ranked observation, fields once, rows reference the facts ---------------


def test_single_ranked_observation_carries_the_five_fields_once() -> None:
    obs = _obs(analyze_farming(_ctx(_drop())))
    # Identity stated once at the observation level.
    assert obs.rule_id == RULE_ID
    assert obs.category == "farming"
    assert obs.tag == "sanity_per_item"
    assert 0.0 <= obs.confidence <= 1.0
    assert isinstance(obs.limitations, tuple)
    assert obs.analyzer_version == ANALYZER_VERSION
    # The per-entity data lives in ranking rows, not N observations.
    assert len(obs.ranking) == 1
    row = obs.ranking[0]
    # The row's id REFERENCES the sibling drops facts (never a re-copied number).
    assert row.id == "sugar"
    assert row.name == "Sugar"
    assert row.sanity_per_item == 72.0  # 18 / 0.25


def test_ranking_row_omits_reinstated_numbers() -> None:
    # A row carries only {id, name, sanity_per_item} (+ deviation fields); the
    # sanity_cost / drop_rate / sample_size are NOT re-copied -- they live in the
    # sibling drops list the client joins on via ``id``.
    row = _obs(analyze_farming(_ctx(_drop()))).ranking[0]
    for reinstated in ("sanity_cost", "drop_rate", "sample_size", "times", "quantity"):
        assert not hasattr(row, reinstated)


def test_sanity_per_item_computed_from_typed_fields() -> None:
    # 18 sanity / 0.25 drops-per-run = 72 sanity per item, from the two typed fields.
    row = _obs(analyze_farming(_ctx(_drop(drop_rate=0.25), sanity_cost=18))).ranking[0]
    assert row.sanity_per_item == 72.0


# --- a non-deviating row omits its own confidence/limitation ---------------------


def test_sufficient_sample_row_is_non_deviating() -> None:
    obs = _obs(analyze_farming(_ctx(_drop(sample_size=SAMPLE_SIZE_FLOOR))))
    # Fresh + well-sampled -> the observation baseline is a recommendation-grade
    # confidence and the row inherits it (its own confidence/markers are omitted).
    assert obs.confidence >= 0.5
    row = obs.ranking[0]
    assert row.confidence is None
    assert row.flags == () and row.expired is False
    # No deviating row -> no hoisted sentence at the observation level either.
    assert obs.limitations == ()


# --- a thin / unreported sample -> that row deviates below the floor --------------


def test_thin_sample_row_deviates_with_flag_and_hoisted_sentence() -> None:
    obs = _obs(analyze_farming(_ctx(_drop(sample_size=SAMPLE_SIZE_FLOOR - 1))))
    row = obs.ranking[0]
    # Below the floor the figure is not a recommendation -- row confidence < 0.5.
    assert row.confidence is not None and row.confidence < 0.5
    # The row carries the typed flag; the sentence lives once on the observation.
    assert FLAG_THIN_SAMPLE in row.flags
    assert any("below the" in lim and "floor" in lim for lim in obs.limitations)


def test_missing_sample_size_row_is_unverified_not_zero() -> None:
    # An absent sample size is not treated as a stable rate -- the row deviates
    # with reduced confidence + a flag, never silently accepted as well-sampled.
    obs = _obs(analyze_farming(_ctx(_drop(sample_size=None))))
    row = obs.ranking[0]
    assert row.confidence is not None and row.confidence < 0.5
    assert FLAG_SAMPLE_UNREPORTED in row.flags
    assert any("sample size" in lim and "unverified" in lim for lim in obs.limitations)


# --- expired cache -> the row is downgraded, not fresh ---------------------------


def test_expired_cache_row_downgraded_to_limitation() -> None:
    obs = _obs(analyze_farming(_ctx(_drop(), expired=True)))
    row = obs.ranking[0]
    # An expired figure is never a fresh recommendation.
    assert row.confidence is not None and row.confidence < 0.5
    assert row.expired is True
    assert any("expired" in lim for lim in obs.limitations)


def test_expired_and_thin_sample_row_carries_both_caveats() -> None:
    # When a drop is BOTH expired and below the sample floor, neither cause
    # masks the other -- the row records both markers (stale AND noisy), not just one,
    # and both hoisted sentences ride the observation.
    obs = _obs(analyze_farming(_ctx(_drop(sample_size=SAMPLE_SIZE_FLOOR - 1), expired=True)))
    row = obs.ranking[0]
    assert row.confidence is not None and row.confidence < 0.5
    assert row.expired is True and FLAG_THIN_SAMPLE in row.flags
    assert any("expired" in lim for lim in obs.limitations)
    assert any("below the" in lim and "floor" in lim for lim in obs.limitations)


# --- the deviation sentence is hoisted ONCE, never repeated per row ---------------


def test_v85_thin_sample_sentence_hoisted_once_across_many_rows() -> None:
    # N thin rows -> ONE observation-level sentence + a per-row flag, never
    # the identical sentence verbatim on every row (~20 repeats in the live eval).
    obs = _obs(
        analyze_farming(
            _ctx(*(_drop(f"item_{i}", sample_size=SAMPLE_SIZE_FLOOR - 1) for i in range(5)))
        )
    )
    thin_sentences = [lim for lim in obs.limitations if "floor" in lim]
    assert len(thin_sentences) == 1
    for row in obs.ranking:
        # per-row: the typed flag + the reduced confidence stay; no sentence.
        assert row.flags == (FLAG_THIN_SAMPLE,)
        assert row.confidence is not None and row.confidence < 0.5
        assert not hasattr(row, "limitations")


def test_v85_hoisted_sentences_only_for_conditions_present() -> None:
    # A mixed ranking hoists exactly the sentences for the conditions present, in a
    # deterministic order (expired, thin, unreported), after the base caveats.
    obs = _item_obs(
        analyze_item_farming(
            _item_ctx(
                _stage_drop("a-1", expired=True),
                _stage_drop("b-2", sample_size=None),
                _stage_drop("c-3"),
            )
        )
    )
    lims = list(obs.limitations)
    expired_idx = [i for i, lim in enumerate(lims) if "expired" in lim]
    unreported_idx = [i for i, lim in enumerate(lims) if "unverified" in lim]
    assert len(expired_idx) == 1 and len(unreported_idx) == 1
    assert expired_idx[0] < unreported_idx[0]
    # thin_sample absent from every row -> its sentence is NOT hoisted.
    assert not any("floor" in lim for lim in lims)


# --- missing inputs -> warning + no observation, never a fabrication --------------


def test_missing_sanity_cost_warns_and_makes_no_observation() -> None:
    analysis = analyze_farming(_ctx(_drop(), sanity_cost=None))
    assert analysis.observation is None
    assert any("sanity cost" in w for w in analysis.warnings)


def test_absent_or_zero_drop_rate_warns_per_item_no_divide_by_zero() -> None:
    analysis = analyze_farming(_ctx(_drop("a", drop_rate=None), _drop("b", drop_rate=0.0)))
    assert analysis.observation is None
    assert any("a: drop rate" in w for w in analysis.warnings)
    assert any("b: drop rate" in w for w in analysis.warnings)


# --- conservative, no prescriptive language --------------------------------------


def test_no_prescriptive_language() -> None:
    obs = _obs(analyze_farming(_ctx(_drop())))
    blob = f"{obs.title} {obs.summary} {' '.join(obs.limitations)}".lower()
    assert not any(word in blob for word in _PRESCRIPTIVE)


# --- the ranking is ascending by sanity per item -------------------------------


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
    # Equal sanity per item -> tie-broken by item_game_id, deterministically.
    obs = _obs(analyze_farming(_ctx(_drop("zzz"), _drop("aaa"), _drop("mmm"))))
    assert [row.id for row in obs.ranking] == ["aaa", "mmm", "zzz"]


# --- item -> stage comparison (reverse of the stage view) ----------------------


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
    # Lowest sanity-per-item first. 4-4 costs 18/0.25=72; a-1 costs 6/0.5=12;
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
    # The row id is the unambiguous stage_game_id; the stage_code rides as name.
    assert ids == ["level_a-1", "level_4-4", "level_b-2"]
    assert [row.name for row in obs.ranking] == ["a-1", "4-4", "b-2"]
    assert figures == [12.0, 72.0, 120.0]
    assert figures == sorted(figures)


def test_item_comparison_carries_mandatory_limitations_on_observation() -> None:
    # The mandatory availability / first-clear / byproduct caveats ride the
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
    # The warning names the unambiguous stage_game_id with the stage_code alongside.
    assert any("level_4-4 (4-4): drop rate" in w for w in analysis.warnings)


def test_item_comparison_expired_stage_kept_not_dropped() -> None:
    # An expired stage's figure is downgraded (its row deviates) but stays IN
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
    assert set(rows) == {"level_4-4", "level_a-1"}  # the expired stage is still present
    expired_row = rows["level_a-1"]
    assert expired_row.confidence is not None and expired_row.confidence < 0.5
    assert expired_row.expired is True
    # The expiry sentence is hoisted once onto the observation, not on the row.
    assert any("expired" in lim for lim in obs.limitations)
    # the fresh stage is non-deviating (inherits the baseline)
    fresh = rows["level_4-4"]
    assert fresh.confidence is None and fresh.flags == () and fresh.expired is False


def test_item_comparison_excludes_missing_inputs_with_warning() -> None:
    # A stage with a missing sanity_cost or absent drop rate is excluded with a
    # warning, never a fabricated figure or a divide-by-zero.
    analysis = analyze_item_farming(
        _item_ctx(
            _stage_drop("ok-1"),
            _stage_drop("no-sanity", sanity_cost=None),
            _stage_drop("no-rate", drop_rate=0.0),
        )
    )
    ids = [row.id for row in _item_obs(analysis).ranking]
    assert ids == ["level_ok-1"]
    assert any("level_no-sanity (no-sanity): stage sanity cost" in w for w in analysis.warnings)
    assert any("level_no-rate (no-rate): drop rate" in w for w in analysis.warnings)


def test_item_comparison_observation_carries_five_fields() -> None:
    # The ranked observation is fully attributed; the row id = the STAGE game_id.
    analysis = analyze_item_farming(_item_ctx(_stage_drop("4-4")))
    obs = _item_obs(analysis)
    assert obs.rule_id == RULE_ID
    assert obs.category == "farming"
    assert 0.0 <= obs.confidence <= 1.0
    assert obs.analyzer_version == ANALYZER_VERSION
    assert [row.id for row in obs.ranking] == ["level_4-4"]
    assert analysis.analyzer_version == ANALYZER_VERSION


def test_item_comparison_no_prescriptive_language() -> None:
    # An ordering + evidence, never a "best farm"/mandatory verdict.
    obs = _item_obs(analyze_item_farming(_item_ctx(_stage_drop("4-4"), _stage_drop("a-1"))))
    blob = f"{obs.title} {obs.summary} {' '.join(obs.limitations)}".lower()
    assert not any(word in blob for word in _PRESCRIPTIVE)


def test_item_comparison_ties_broken_deterministically() -> None:
    # Equal-cost stages rank by stage_code then game_id, deterministically.
    obs = _item_obs(
        analyze_item_farming(
            _item_ctx(
                _stage_drop("z-9", sanity_cost=18, drop_rate=0.25),
                _stage_drop("a-1", sanity_cost=18, drop_rate=0.25),
                _stage_drop("m-5", sanity_cost=18, drop_rate=0.25),
            )
        )
    )
    assert [row.id for row in obs.ranking] == ["level_a-1", "level_m-5", "level_z-9"]


# --- evidence ref is the unambiguous stage_game_id, code shown alongside ---------


def test_v68_item_ref_is_stage_game_id_not_shared_code() -> None:
    # Two stages sharing stage_code "14-18" (a normal + tough pair) must get
    # DISTINCT refs -- the unambiguous stage_game_id -- with the shared code shown
    # alongside as ``name``, so the refs join 1:1 to the sibling stages facts list
    # (which keys on stage_game_id) instead of colliding on one undecidable "14-18".
    obs = _item_obs(
        analyze_item_farming(
            _item_ctx(
                _stage_drop("14-18", stage_game_id="main_10-09", sanity_cost=18, drop_rate=0.25),
                _stage_drop("14-18", stage_game_id="tough_10-09", sanity_cost=36, drop_rate=0.25),
            )
        )
    )
    refs = [row.id for row in obs.ranking]
    # main_10-09 = 18/0.25 = 72, tough_10-09 = 36/0.25 = 144 -> ascending main then tough.
    assert refs == ["main_10-09", "tough_10-09"]
    assert len(set(refs)) == 2  # two DISTINCT refs, not one ambiguous "14-18"
    # The shared stage_code rides alongside as the display name, never as the ref.
    assert all(row.name == "14-18" for row in obs.ranking)
    assert "14-18" not in refs


# --- the stage view and the item comparison share ONE math + confidence core -------


def test_v37_stage_and_item_views_agree_on_figure_and_confidence() -> None:
    # analyze_farming (stage view) and analyze_item_farming (item view) compute
    # the sanity-per-item figure and the confidence ladder in exactly one
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
        # the same conservatism markers (expiry / thin / unreported) fire in both
        assert stage_row.expired == item_row.expired
        assert stage_row.flags == item_row.flags
