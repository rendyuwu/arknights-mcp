"""Module analyzer tests.

Drive :func:`~arknights_mcp.analyzers.module.analyze_modules` directly over typed,
DB-free contexts (the compare service builds these from vetted structural
JSON). The analyzer is pure, so these assert the deterministic contract without a
database:

* every observation carries the five attribution fields (rule_id + evidence + confidence +
  limitations + analyzer_version), with typed-field evidence only;
* an absent requested level is a warning, never a false/zero conclusion;
* a module with no typed changes yields no observation (missing != zero);
* summaries state capability facts, never a "mandatory"/"best" verdict;
* modules + evidence are emitted in a stable order (deterministic).

ADR 0018: the trait and talent rules are RETIRED, so the tests
that pinned their output are gone with them and one guard is here in their place --
the analyzer emits stat observations only. The retired rules restated facts the same
payload already carried (which levels alter the trait, which talent index each change
targets, both readable straight off the emitted change rows), so 6 of the 9
observations a three-module operator received were re-readings. ``module.stat_bonus``
stays because its delta is COMPUTED: the rows carry absolute bonuses per level and
never the step between them.
"""

from __future__ import annotations

from arknights_mcp.analyzers import ANALYZER_VERSION
from arknights_mcp.analyzers.module import (
    ModuleAnalysisContext,
    ModuleInput,
    ModuleLevelInput,
    ModuleStat,
    analyze_modules,
)

_PRESCRIPTIVE = ("mandatory", "best-in-slot", "best in slot", "must ", "should ", "always use")

#: The rule ids ADR 0018 retired. Named here so the guard below fails on a
#: reintroduction rather than on a count that some later rule could restore.
_RETIRED_RULE_IDS = ("module.trait_change", "module.talent_change")


def _amiya_cx1(levels: tuple[ModuleLevelInput, ...]) -> ModuleInput:
    return ModuleInput(
        game_id="uniequip_002_amiya",
        module_type="CX-1",
        display_name="Magician's Trick",
        levels=levels,
    )


def _full_cx1() -> ModuleInput:
    """CX-1 with all three real levels: atk 34/48/66, plus 150 max_hp at level 3."""
    return _amiya_cx1(
        (
            ModuleLevelInput(level=1, present=True, stats=(ModuleStat(key="atk", value=34.0),)),
            ModuleLevelInput(level=2, present=True, stats=(ModuleStat(key="atk", value=48.0),)),
            ModuleLevelInput(
                level=3,
                present=True,
                stats=(ModuleStat(key="atk", value=66.0), ModuleStat(key="max_hp", value=150.0)),
            ),
        )
    )


def _ctx(*modules: ModuleInput, levels: tuple[int, ...] = (1, 2, 3)) -> ModuleAnalysisContext:
    return ModuleAnalysisContext(
        server="en",
        operator_game_id="char_002_amiya",
        requested_levels=levels,
        modules=tuple(modules),
    )


def _by_tag(analysis) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {o.tag: o for o in analysis.observations}


# --- every observation is fully attributed --------------------------------------


def test_stat_observation_emitted_and_fully_attributed() -> None:
    analysis = analyze_modules(_ctx(_full_cx1()))
    assert set(_by_tag(analysis)) == {"stat_bonus"}
    for obs in analysis.observations:
        # rule_id + evidence + confidence + limitations + analyzer_version.
        assert obs.rule_id.startswith("module.")
        assert obs.category == "module"
        assert obs.evidence  # never a bare verdict
        assert 0.0 <= obs.confidence <= 1.0
        assert isinstance(obs.limitations, tuple)
        assert obs.analyzer_version == ANALYZER_VERSION
        for ev in obs.evidence:
            assert ev.ref == "uniequip_002_amiya" and ev.field


def test_stat_observation_reports_cross_level_diff() -> None:
    # The per-level absolute bonuses are visible in the stat_bonus rows, so
    # the observation reports only the computed cross-level delta -- atk +14 (Lv1->2), +18
    # (Lv2->3). max_hp appears at a single level so it contributes no delta.
    stat = _by_tag(analyze_modules(_ctx(_full_cx1())))["stat_bonus"]
    seen = {(ev.field, ev.value) for ev in stat.evidence}  # type: ignore[attr-defined]
    assert ("stat_bonus.atk", 14.0) in seen  # 48 - 34
    assert ("stat_bonus.atk", 18.0) in seen  # 66 - 48
    assert not any(ev.field == "stat_bonus.max_hp" for ev in stat.evidence)  # single level
    # The summary states the signed change, never the raw absolute already in the rows.
    assert "+14" in stat.summary and "+18" in stat.summary  # type: ignore[attr-defined]
    assert "34" not in stat.summary  # type: ignore[attr-defined]


def test_single_level_stat_bonus_yields_no_observation() -> None:
    # A stat bonus at a single level is fully visible in its stat_bonus row,
    # so there is no cross-level change to compute -> no observation (never restate a row).
    one = _amiya_cx1(
        (ModuleLevelInput(level=1, present=True, stats=(ModuleStat(key="atk", value=34.0),)),)
    )
    analysis = analyze_modules(_ctx(one, levels=(1,)))
    assert "stat_bonus" not in {o.tag for o in analysis.observations}


def test_constant_stat_across_levels_yields_no_observation() -> None:
    # A stat that holds constant across two present levels (atk 34 -> 34) is
    # not a change; emitting a "+0" step would restate a non-change as a change. With the
    # only stat constant, there is no evidence -> no observation (never a bare "+0").
    flat = _amiya_cx1(
        (
            ModuleLevelInput(level=1, present=True, stats=(ModuleStat(key="atk", value=34.0),)),
            ModuleLevelInput(level=2, present=True, stats=(ModuleStat(key="atk", value=34.0),)),
        )
    )
    analysis = analyze_modules(_ctx(flat, levels=(1, 2)))
    assert "stat_bonus" not in {o.tag for o in analysis.observations}
    assert all("+0" not in o.summary for o in analysis.observations)


def test_constant_stat_skipped_but_changing_stat_kept() -> None:
    # The skip is per-stat, not all-or-nothing -- atk changes (34 -> 48) so its
    # +14 delta is kept, while def holds constant (10 -> 10) and contributes no evidence and
    # no "+0" in the summary.
    mixed = _amiya_cx1(
        (
            ModuleLevelInput(
                level=1,
                present=True,
                stats=(ModuleStat(key="atk", value=34.0), ModuleStat(key="def", value=10.0)),
            ),
            ModuleLevelInput(
                level=2,
                present=True,
                stats=(ModuleStat(key="atk", value=48.0), ModuleStat(key="def", value=10.0)),
            ),
        )
    )
    stat = _by_tag(analyze_modules(_ctx(mixed, levels=(1, 2))))["stat_bonus"]
    seen = {(ev.field, ev.value) for ev in stat.evidence}  # type: ignore[attr-defined]
    assert ("stat_bonus.atk", 14.0) in seen  # 48 - 34
    assert not any(ev.field == "stat_bonus.def" for ev in stat.evidence)  # type: ignore[attr-defined]
    assert "+14" in stat.summary and "def" not in stat.summary  # type: ignore[attr-defined]
    assert "+0" not in stat.summary  # type: ignore[attr-defined]


# --- ADR 0018: the two padded rules are retired ---------------------------------


def test_retired_trait_and_talent_rules_emit_nothing() -> None:
    # A module that alters the trait at every level and overrides two talents used to
    # emit two extra observations restating exactly that -- both facts are already in the
    # emitted trait_changes / talent_changes rows the same response carries (evidence
    # never re-states numbers already in sibling facts). The analyzer input no longer even
    # carries the change lists, so the guard asserts the OUTPUT: whatever the levels hold,
    # the only rule that fires is the computed stat delta.
    analysis = analyze_modules(_ctx(_full_cx1()))
    assert {o.rule_id for o in analysis.observations} == {"module.stat_bonus"}
    for retired in _RETIRED_RULE_IDS:
        assert all(o.rule_id != retired for o in analysis.observations)
        assert all(o.tag != retired.removeprefix("module.") for o in analysis.observations)


def test_module_whose_only_content_is_changes_yields_no_observation() -> None:
    # The other half of the retirement: a module with change bundles but no stat movement
    # now yields NOTHING rather than two restating rows. The facts still ship -- the change
    # bundles are in the payload beside this empty observations list (disclosure is
    # the response's job, not a rule's).
    changes_only = _amiya_cx1(
        (
            ModuleLevelInput(level=1, present=True, stats=()),
            ModuleLevelInput(level=2, present=True, stats=()),
        )
    )
    analysis = analyze_modules(_ctx(changes_only, levels=(1, 2)))
    assert analysis.observations == ()


# --- absent level -> warning, missing != zero -----------------------------------


def test_absent_requested_level_is_a_warning_not_a_conclusion() -> None:
    # A module defined at levels 1-2 but level 3 requested: the missing level is warned,
    # never reported as an empty/zero change, and forms no cross-level delta.
    partial = _amiya_cx1(
        (
            ModuleLevelInput(level=1, present=True, stats=(ModuleStat(key="atk", value=34.0),)),
            ModuleLevelInput(level=2, present=True, stats=(ModuleStat(key="atk", value=48.0),)),
            ModuleLevelInput(level=3, present=False, stats=()),
        )
    )
    analysis = analyze_modules(_ctx(partial, levels=(1, 2, 3)))
    assert any("level 3 is not defined" in w for w in analysis.warnings)
    stat = _by_tag(analysis)["stat_bonus"]
    # Only the two present levels form a delta (48 - 34); the absent level adds nothing.
    assert {ev.value for ev in stat.evidence} == {14.0}  # type: ignore[attr-defined]
    assert "+14" in stat.summary  # type: ignore[attr-defined]


def test_module_with_no_typed_changes_yields_no_observation() -> None:
    # Absent typed data is not a zero conclusion -- a bare module produces no
    # observation (and no warning, since every requested level is present).
    bare = _amiya_cx1((ModuleLevelInput(level=1, present=True, stats=()),))
    analysis = analyze_modules(_ctx(bare, levels=(1,)))
    assert analysis.observations == ()
    assert analysis.warnings == ()


# --- conservative, no prescriptive language -------------------------------------


def test_no_prescriptive_language() -> None:
    analysis = analyze_modules(_ctx(_full_cx1()))
    for obs in analysis.observations:
        blob = f"{obs.title} {obs.summary}".lower()
        assert not any(word in blob for word in _PRESCRIPTIVE)


# --- deterministic order -------------------------------------------------------


def test_modules_processed_in_supplied_order() -> None:
    # Two modules -> observations grouped in the order the service supplies them
    # (it orders by game_id), so output is deterministic.
    a = ModuleInput(
        game_id="uniequip_002_a",
        module_type="AA-1",
        display_name=None,
        levels=(
            ModuleLevelInput(level=1, present=True, stats=(ModuleStat(key="atk", value=10.0),)),
            ModuleLevelInput(level=2, present=True, stats=(ModuleStat(key="atk", value=20.0),)),
        ),
    )
    b = ModuleInput(
        game_id="uniequip_003_b",
        module_type="BB-1",
        display_name=None,
        levels=(
            ModuleLevelInput(level=1, present=True, stats=(ModuleStat(key="def", value=5.0),)),
            ModuleLevelInput(level=2, present=True, stats=(ModuleStat(key="def", value=12.0),)),
        ),
    )
    refs = [obs.evidence[0].ref for obs in analyze_modules(_ctx(a, b, levels=(1, 2))).observations]
    assert refs == ["uniequip_002_a", "uniequip_003_b"]
