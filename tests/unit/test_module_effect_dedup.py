"""T168 (§V83/§V66/B88): the shared module trait/talent change dedup + token-label + the
per-level cross-hoist helpers.

The module read services (:func:`~arknights_mcp.services.operators.get_operator` modules and
:func:`~arknights_mcp.services.module_compare.compare_operator_modules`) route every emitted
talent/trait change list through these single §V37 homes so a module no longer emits N
near-identical rows for one change (B88), a ``-1`` summon/token change is labelled instead of
left bare, and a change bundle byte-identical across every level rides the module once.
"""

from __future__ import annotations

from arknights_mcp.services.operators import (
    dedup_and_label_changes,
    dedup_effect_changes,
    hoist_uniform_changes,
    label_token_effects,
)

_COND = {"phase": "PHASE_2", "level": 1}
_BB = [{"key": "atk", "value": 1}]


# --- dedup_effect_changes: collapse duplicate/subset rows (§V83/§V66) ----------


def test_six_redundant_rows_for_one_talent_collapse_to_one() -> None:
    # B88: Kal'tsit Mon3tr had 6 talent_changes for ONE talent -- 2 prose-only, 2
    # blackboard+prose duplicates, 2 blackboard-only. They share one identity
    # (talentIndex/potential/unlock), so they merge into a SINGLE row carrying the union of
    # the non-empty fields (blackboard + description). Byte-lossless: no field is lost.
    prose_only = {
        "talentIndex": -1,
        "requiredPotentialRank": 0,
        "unlockCondition": _COND,
        "description": "D",
    }
    both = {**prose_only, "blackboard": _BB}
    bb_only = {
        "talentIndex": -1,
        "requiredPotentialRank": 0,
        "unlockCondition": _COND,
        "blackboard": _BB,
    }
    merged = dedup_effect_changes([prose_only, prose_only, both, both, bb_only, bb_only])
    assert merged == [
        {
            "talentIndex": -1,
            "requiredPotentialRank": 0,
            "unlockCondition": _COND,
            "description": "D",
            "blackboard": _BB,
        }
    ]


def test_byte_identical_rows_collapse() -> None:
    row = {
        "talentIndex": 0,
        "requiredPotentialRank": 0,
        "unlockCondition": _COND,
        "blackboard": _BB,
    }
    assert dedup_effect_changes([row, row, row]) == [row]


def test_conflicting_blackboards_under_one_identity_stay_separate() -> None:
    # A genuine conflict (two DIFFERENT non-empty blackboards under the same gate) is not a
    # duplicate -- merging would drop data, so both rows survive (byte-lossless).
    a = {
        "talentIndex": 0,
        "requiredPotentialRank": 0,
        "unlockCondition": _COND,
        "blackboard": [{"key": "atk", "value": 1}],
    }
    b = {
        "talentIndex": 0,
        "requiredPotentialRank": 0,
        "unlockCondition": _COND,
        "blackboard": [{"key": "atk", "value": 2}],
    }
    assert dedup_effect_changes([a, b]) == [a, b]


def test_different_identity_rows_are_not_merged() -> None:
    # Different potential rank -> different change -> kept as separate rows.
    a = {"talentIndex": 0, "requiredPotentialRank": 0, "unlockCondition": _COND, "blackboard": _BB}
    b = {"talentIndex": 0, "requiredPotentialRank": 1, "unlockCondition": _COND, "blackboard": _BB}
    assert dedup_effect_changes([a, b]) == [a, b]


def test_dedup_preserves_first_appearance_order() -> None:
    x = {"talentIndex": 0, "requiredPotentialRank": 0, "unlockCondition": _COND}
    y = {"talentIndex": 1, "requiredPotentialRank": 0, "unlockCondition": _COND}
    assert dedup_effect_changes([x, y, x]) == [x, y]


def test_dedup_non_list_passthrough() -> None:
    assert dedup_effect_changes(None) is None
    assert dedup_effect_changes(42) == 42


# --- label_token_effects: label the -1 summon/token change (§V83) --------------


def test_token_talent_index_gets_applies_to_label() -> None:
    out = label_token_effects([{"talentIndex": -1, "blackboard": _BB}])
    assert out == [{"talentIndex": -1, "blackboard": _BB, "applies_to": "token"}]


def test_non_token_talent_index_untouched() -> None:
    row = {"talentIndex": 0, "blackboard": _BB}
    assert label_token_effects([row]) == [row]


def test_label_is_idempotent_and_passes_through_non_list() -> None:
    already = {"talentIndex": -1, "applies_to": "token"}
    assert label_token_effects([already]) == [already]
    assert label_token_effects(None) is None


def test_dedup_and_label_composes_both_steps() -> None:
    # The service pipeline: collapse duplicates, THEN label the surviving -1 row.
    row = {
        "talentIndex": -1,
        "requiredPotentialRank": 0,
        "unlockCondition": _COND,
        "blackboard": _BB,
    }
    assert dedup_and_label_changes([row, row]) == [{**row, "applies_to": "token"}]


# --- hoist_uniform_changes: cross-level bundle hoist (§V66.3/§V83) --------------


def test_identical_bundle_across_levels_is_hoisted() -> None:
    bundle = [{"blackboard": _BB, "requiredPotentialRank": 0, "unlockCondition": _COND}]
    assert hoist_uniform_changes([bundle, bundle, bundle]) == bundle


def test_hoist_declines_when_a_level_lacks_the_bundle() -> None:
    bundle = [{"blackboard": _BB}]
    # A None (level carried no change) or an empty list is not uniform -> keep per level.
    assert hoist_uniform_changes([bundle, None]) is None
    assert hoist_uniform_changes([bundle, []]) is None


def test_hoist_declines_when_bundles_differ() -> None:
    assert (
        hoist_uniform_changes(
            [
                [{"blackboard": [{"key": "atk", "value": 1}]}],
                [{"blackboard": [{"key": "atk", "value": 2}]}],
            ]
        )
        is None
    )


def test_hoist_needs_at_least_two_levels() -> None:
    assert hoist_uniform_changes([[{"blackboard": _BB}]]) is None
    assert hoist_uniform_changes([]) is None
