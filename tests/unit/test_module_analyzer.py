"""§T46 module analyzer tests (§V6, §V7, §V26).

Drive :func:`~arknights_mcp.analyzers.module.analyze_modules` directly over typed,
DB-free contexts (the compare service, §T45, builds these from vetted structural
JSON). The analyzer is pure, so these assert the deterministic contract without a
database:

* every observation carries the five §V6 fields (rule_id + evidence + confidence +
  limitations + analyzer_version), with typed-field evidence only (§V26);
* an absent requested level is a §V26 warning, never a false/zero conclusion;
* a module with no typed changes yields no observation (missing != zero, §V26);
* summaries state capability facts, never a "mandatory"/"best" verdict (§V7);
* modules + evidence are emitted in a stable order (deterministic, §V26).
"""

from __future__ import annotations

from arknights_mcp.analyzers import ANALYZER_VERSION
from arknights_mcp.analyzers.module import (
    ModuleAnalysisContext,
    ModuleInput,
    ModuleLevelInput,
    ModuleStat,
    ModuleTalentChange,
    analyze_modules,
)

_PRESCRIPTIVE = ("mandatory", "best-in-slot", "best in slot", "must ", "should ", "always use")


def _amiya_cx1(levels: tuple[ModuleLevelInput, ...]) -> ModuleInput:
    return ModuleInput(
        game_id="uniequip_002_amiya",
        module_type="CX-1",
        display_name="Magician's Trick",
        levels=levels,
    )


def _full_cx1() -> ModuleInput:
    """CX-1 with all three real levels: atk 34/48/66 (+150 hp at 3), trait@1, talent@2."""
    return _amiya_cx1(
        (
            ModuleLevelInput(
                level=1,
                present=True,
                stats=(ModuleStat(key="atk", value=34.0),),
                trait_change_count=1,
                talent_changes=(),
            ),
            ModuleLevelInput(
                level=2,
                present=True,
                stats=(ModuleStat(key="atk", value=48.0),),
                trait_change_count=0,
                talent_changes=(ModuleTalentChange(talent_index=0),),
            ),
            ModuleLevelInput(
                level=3,
                present=True,
                stats=(ModuleStat(key="atk", value=66.0), ModuleStat(key="max_hp", value=150.0)),
                trait_change_count=0,
                talent_changes=(),
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


# --- §V6: every observation is fully attributed --------------------------------


def test_stat_talent_trait_observations_emitted() -> None:
    analysis = analyze_modules(_ctx(_full_cx1()))
    tags = _by_tag(analysis)
    assert set(tags) == {"stat_bonus", "trait_change", "talent_change"}
    for obs in analysis.observations:
        # §V6: rule_id + evidence + confidence + limitations + analyzer_version.
        assert obs.rule_id.startswith("module.")
        assert obs.category == "module"
        assert obs.evidence  # never a bare verdict
        assert 0.0 <= obs.confidence <= 1.0
        assert isinstance(obs.limitations, tuple)
        assert obs.analyzer_version == ANALYZER_VERSION
        for ev in obs.evidence:
            assert ev.ref == "uniequip_002_amiya" and ev.field


def test_stat_observation_reports_cross_level_diff() -> None:
    # §T148/§V66.1: the per-level absolute bonuses are visible in the stat_bonus rows, so
    # the observation reports only the computed cross-level delta -- atk +14 (Lv1->2), +18
    # (Lv2->3). max_hp appears at a single level so it contributes no delta (§V26).
    stat = _by_tag(analyze_modules(_ctx(_full_cx1())))["stat_bonus"]
    seen = {(ev.field, ev.value) for ev in stat.evidence}  # type: ignore[attr-defined]
    assert ("stat_bonus.atk", 14.0) in seen  # 48 - 34
    assert ("stat_bonus.atk", 18.0) in seen  # 66 - 48
    assert not any(ev.field == "stat_bonus.max_hp" for ev in stat.evidence)  # single level
    # The summary states the signed change, never the raw absolute already in the rows.
    assert "+14" in stat.summary and "+18" in stat.summary  # type: ignore[attr-defined]
    assert "34" not in stat.summary  # type: ignore[attr-defined]


def test_single_level_stat_bonus_yields_no_observation() -> None:
    # §T148/§V66.1: a stat bonus at a single level is fully visible in its stat_bonus row,
    # so there is no cross-level change to compute -> no observation (never restate a row).
    one = _amiya_cx1(
        (
            ModuleLevelInput(
                level=1,
                present=True,
                stats=(ModuleStat(key="atk", value=34.0),),
                trait_change_count=0,
                talent_changes=(),
            ),
        )
    )
    analysis = analyze_modules(_ctx(one, levels=(1,)))
    assert "stat_bonus" not in {o.tag for o in analysis.observations}


def test_constant_stat_across_levels_yields_no_observation() -> None:
    # §V66.1/B75: a stat that holds constant across two present levels (atk 34 -> 34) is
    # not a change; emitting a "+0" step would restate a non-change as a change. With the
    # only stat constant, there is no evidence -> no observation (never a bare "+0").
    flat = _amiya_cx1(
        (
            ModuleLevelInput(
                level=1,
                present=True,
                stats=(ModuleStat(key="atk", value=34.0),),
                trait_change_count=0,
                talent_changes=(),
            ),
            ModuleLevelInput(
                level=2,
                present=True,
                stats=(ModuleStat(key="atk", value=34.0),),
                trait_change_count=0,
                talent_changes=(),
            ),
        )
    )
    analysis = analyze_modules(_ctx(flat, levels=(1, 2)))
    assert "stat_bonus" not in {o.tag for o in analysis.observations}
    assert all("+0" not in o.summary for o in analysis.observations)


def test_constant_stat_skipped_but_changing_stat_kept() -> None:
    # §V66.1/B75: the skip is per-stat, not all-or-nothing -- atk changes (34 -> 48) so its
    # +14 delta is kept, while def holds constant (10 -> 10) and contributes no evidence and
    # no "+0" in the summary.
    mixed = _amiya_cx1(
        (
            ModuleLevelInput(
                level=1,
                present=True,
                stats=(ModuleStat(key="atk", value=34.0), ModuleStat(key="def", value=10.0)),
                trait_change_count=0,
                talent_changes=(),
            ),
            ModuleLevelInput(
                level=2,
                present=True,
                stats=(ModuleStat(key="atk", value=48.0), ModuleStat(key="def", value=10.0)),
                trait_change_count=0,
                talent_changes=(),
            ),
        )
    )
    stat = _by_tag(analyze_modules(_ctx(mixed, levels=(1, 2))))["stat_bonus"]
    seen = {(ev.field, ev.value) for ev in stat.evidence}  # type: ignore[attr-defined]
    assert ("stat_bonus.atk", 14.0) in seen  # 48 - 34
    assert not any(ev.field == "stat_bonus.def" for ev in stat.evidence)  # type: ignore[attr-defined]
    assert "+14" in stat.summary and "def" not in stat.summary  # type: ignore[attr-defined]
    assert "+0" not in stat.summary  # type: ignore[attr-defined]


def test_talent_observation_names_the_typed_index() -> None:
    talent = _by_tag(analyze_modules(_ctx(_full_cx1())))["talent_change"]
    assert any(ev.value == 0 for ev in talent.evidence)  # type: ignore[attr-defined]
    assert "0" in talent.summary  # type: ignore[attr-defined]


def _token_module(talents: tuple[ModuleTalentChange, ...]) -> ModuleInput:
    # module_type=None so the label falls back to the -1-free game_id -- a "-1" in the
    # summary then can only be a leaked talentIndex, not the module type (e.g. "CX-1").
    return ModuleInput(
        game_id="uniequip_002_amiya",
        module_type=None,
        display_name=None,
        levels=(
            ModuleLevelInput(
                level=1, present=True, stats=(), trait_change_count=0, talent_changes=talents
            ),
        ),
    )


def test_talent_observation_glosses_token_effect_index() -> None:
    # §T148/§V71: talentIndex -1 is the token/summon effect convention, not a numbered
    # talent; the observation glosses it as "the token effect" and leaks no bare -1 --
    # neither in the summary nor the evidence (a typed token_effect flag instead).
    module = _token_module((ModuleTalentChange(talent_index=-1),))
    talent = _by_tag(analyze_modules(_ctx(module, levels=(1,))))["talent_change"]
    assert "token effect" in talent.summary  # type: ignore[attr-defined]
    assert "-1" not in talent.summary  # type: ignore[attr-defined]
    assert all(ev.value != -1 for ev in talent.evidence)  # type: ignore[attr-defined]
    assert any(ev.field == "talent_changes.token_effect" for ev in talent.evidence)  # type: ignore[attr-defined]


def test_talent_observation_combines_numbered_and_token_effect() -> None:
    # §T148: a module that changes a numbered talent AND the token effect names both,
    # with the -1 still glossed (numbered talents first, token effect appended).
    module = _token_module(
        (ModuleTalentChange(talent_index=0), ModuleTalentChange(talent_index=-1))
    )
    talent = _by_tag(analyze_modules(_ctx(module, levels=(1,))))["talent_change"]
    assert "talent(s) 0" in talent.summary  # type: ignore[attr-defined]
    assert "the token effect" in talent.summary  # type: ignore[attr-defined]
    assert "-1" not in talent.summary  # type: ignore[attr-defined]


# --- §V26: absent level -> warning, missing != zero ----------------------------


def test_absent_requested_level_is_a_warning_not_a_conclusion() -> None:
    # A module defined at levels 1-2 but level 3 requested: the missing level is warned,
    # never reported as an empty/zero change (§V26), and forms no cross-level delta.
    partial = _amiya_cx1(
        (
            ModuleLevelInput(
                level=1,
                present=True,
                stats=(ModuleStat(key="atk", value=34.0),),
                trait_change_count=0,
                talent_changes=(),
            ),
            ModuleLevelInput(
                level=2,
                present=True,
                stats=(ModuleStat(key="atk", value=48.0),),
                trait_change_count=0,
                talent_changes=(),
            ),
            ModuleLevelInput(
                level=3, present=False, stats=(), trait_change_count=0, talent_changes=()
            ),
        )
    )
    analysis = analyze_modules(_ctx(partial, levels=(1, 2, 3)))
    assert any("level 3 is not defined" in w for w in analysis.warnings)
    stat = _by_tag(analysis)["stat_bonus"]
    # Only the two present levels form a delta (48 - 34); the absent level adds nothing.
    assert {ev.value for ev in stat.evidence} == {14.0}  # type: ignore[attr-defined]
    assert "+14" in stat.summary  # type: ignore[attr-defined]


def test_module_with_no_typed_changes_yields_no_observation() -> None:
    # §V26: absent typed data is not a zero conclusion -- a bare module produces no
    # observation (and no warning, since every requested level is present).
    bare = _amiya_cx1(
        (
            ModuleLevelInput(
                level=1, present=True, stats=(), trait_change_count=0, talent_changes=()
            ),
        )
    )
    analysis = analyze_modules(_ctx(bare, levels=(1,)))
    assert analysis.observations == ()
    assert analysis.warnings == ()


# --- §V7: conservative, no prescriptive language -------------------------------


def test_no_prescriptive_language() -> None:
    analysis = analyze_modules(_ctx(_full_cx1()))
    for obs in analysis.observations:
        blob = f"{obs.title} {obs.summary}".lower()
        assert not any(word in blob for word in _PRESCRIPTIVE)


# --- deterministic order -------------------------------------------------------


def test_modules_processed_in_supplied_order() -> None:
    # Two modules -> observations grouped in the order the service supplies them
    # (it orders by game_id), so output is deterministic (§V26).
    a = ModuleInput(
        game_id="uniequip_002_a",
        module_type="AA-1",
        display_name=None,
        levels=(
            ModuleLevelInput(
                level=1,
                present=True,
                stats=(ModuleStat(key="atk", value=10.0),),
                trait_change_count=0,
                talent_changes=(),
            ),
            ModuleLevelInput(
                level=2,
                present=True,
                stats=(ModuleStat(key="atk", value=20.0),),
                trait_change_count=0,
                talent_changes=(),
            ),
        ),
    )
    b = ModuleInput(
        game_id="uniequip_003_b",
        module_type="BB-1",
        display_name=None,
        levels=(
            ModuleLevelInput(
                level=1,
                present=True,
                stats=(ModuleStat(key="def", value=5.0),),
                trait_change_count=0,
                talent_changes=(),
            ),
            ModuleLevelInput(
                level=2,
                present=True,
                stats=(ModuleStat(key="def", value=12.0),),
                trait_change_count=0,
                talent_changes=(),
            ),
        ),
    )
    refs = [obs.evidence[0].ref for obs in analyze_modules(_ctx(a, b, levels=(1, 2))).observations]
    assert refs == ["uniequip_002_a", "uniequip_003_b"]
