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


def test_stat_evidence_traces_each_typed_level_value() -> None:
    # §V26: the stat observation reads typed key/value pairs -- one evidence row per
    # (level, stat), so the whole progression is auditable.
    stat = _by_tag(analyze_modules(_ctx(_full_cx1())))["stat_bonus"]
    seen = {(ev.field, ev.value) for ev in stat.evidence}  # type: ignore[attr-defined]
    assert ("stat_bonus.atk", 34.0) in seen
    assert ("stat_bonus.atk", 66.0) in seen
    assert ("stat_bonus.max_hp", 150.0) in seen
    # The summary states the values factually (no prescription; §V7).
    assert "atk" in stat.summary and "150" in stat.summary  # type: ignore[attr-defined]


def test_talent_observation_names_the_typed_index() -> None:
    talent = _by_tag(analyze_modules(_ctx(_full_cx1())))["talent_change"]
    assert any(ev.value == 0 for ev in talent.evidence)  # type: ignore[attr-defined]
    assert "0" in talent.summary  # type: ignore[attr-defined]


# --- §V26: absent level -> warning, missing != zero ----------------------------


def test_absent_requested_level_is_a_warning_not_a_conclusion() -> None:
    # A module defined only at level 1 but level 3 requested: the missing level is
    # warned, never reported as an empty/zero change (§V26).
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
                level=3, present=False, stats=(), trait_change_count=0, talent_changes=()
            ),
        )
    )
    analysis = analyze_modules(_ctx(partial, levels=(1, 3)))
    assert any("level 3 is not defined" in w for w in analysis.warnings)
    stat = _by_tag(analysis)["stat_bonus"]
    # Only the present level contributes evidence -- the absent level adds nothing.
    assert {ev.value for ev in stat.evidence} == {34.0}  # type: ignore[attr-defined]


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
                stats=(ModuleStat(key="def", value=20.0),),
                trait_change_count=0,
                talent_changes=(),
            ),
        ),
    )
    refs = [obs.evidence[0].ref for obs in analyze_modules(_ctx(a, b, levels=(1,))).observations]
    assert refs == ["uniequip_002_a", "uniequip_003_b"]
