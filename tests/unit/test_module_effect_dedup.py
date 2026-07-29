"""T168 (§V83/§V66/B88) + T198 (§V71 d/B140): the shared module trait/talent change dedup,
token-label, per-level cross-hoist, and wire-key normalization helpers.

The module read services (:func:`~arknights_mcp.services.operators.get_operator` modules and
:func:`~arknights_mcp.services.module_compare.compare_operator_modules`) route every emitted
talent/trait change list through these single §V37 homes so a module no longer emits N
near-identical rows for one change (B88), a ``-1`` summon/token change is labelled instead of
left bare, a change bundle byte-identical across every level rides the module once, and the
source's camelCase keys plus its doubly-encoded unlock phase are normalized before the wire.

The ORDER matters and is asserted below: dedup and labelling read the SOURCE key names, the
rename runs last. Reversing them would leave the dedup identity keying on names the source
never sends, so every bundle would look distinct and B88 would silently return.
"""

from __future__ import annotations

from arknights_mcp.services.effect_changes import (
    dedup_and_label_changes,
    dedup_effect_changes,
    hoist_uniform_changes,
    label_token_effects,
    normalize_change_keys,
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


def test_dedup_and_label_composes_all_three_steps() -> None:
    # The service pipeline: collapse duplicates, label the surviving -1 row, THEN rename
    # the keys for the wire (§V71 d). The rename is last so the first two steps still read
    # the source's own names.
    row = {
        "talentIndex": -1,
        "requiredPotentialRank": 0,
        "unlockCondition": _COND,
        "blackboard": _BB,
    }
    assert dedup_and_label_changes([row, row]) == [
        {
            "talent_index": -1,
            "required_potential_rank": 0,
            "unlock_condition": {"phase": 2, "level": 1},
            "blackboard": _BB,
            "applies_to": "token",
        }
    ]


# --- normalize_change_keys: snake_case + one phase encoding (§V71 d/§V99/B140) --


def test_camelcase_keys_are_renamed_for_the_wire() -> None:
    # §V71 (d)/B140: these three shipped camelCase in a snake_case envelope, three lines
    # from a sibling ``unlock_phase``. The §V named them and the fix lost its vehicle when
    # B69 spent the bump it was gated behind; T198 delivers it.
    out = normalize_change_keys(
        [{"talentIndex": 1, "requiredPotentialRank": 0, "unlockCondition": _COND}]
    )
    assert out == [
        {
            "talent_index": 1,
            "required_potential_rank": 0,
            "unlock_condition": {"phase": 2, "level": 1},
        }
    ]


def test_nested_unlock_phase_collapses_to_one_encoding() -> None:
    # §V99/B140: the same object encoded the phase twice, two ways -- an int
    # ``unlock_phase: 2`` beside ``unlockCondition.phase: "PHASE_2"``. One concept, two
    # types. The nested one becomes the int its sibling already is.
    out = normalize_change_keys([{"unlockCondition": {"phase": "PHASE_2", "level": 60}}])
    assert out == [{"unlock_condition": {"phase": 2, "level": 60}}]


def test_unparsable_phase_is_left_exactly_as_the_source_sent_it() -> None:
    # §V26: an encoding we do not recognize is REPORTED, never guessed at or dropped. A
    # silent None here would delete a fact the source stated.
    assert normalize_change_keys([{"unlockCondition": {"phase": "WEIRD"}}]) == [
        {"unlock_condition": {"phase": "WEIRD"}}
    ]
    # An unlock condition with no phase key at all passes through untouched.
    assert normalize_change_keys([{"unlockCondition": {"level": 60}}]) == [
        {"unlock_condition": {"level": 60}}
    ]


def test_unrelated_keys_and_order_survive_the_rename() -> None:
    # Key ORDER is preserved so a renamed bundle serializes where it always did, and a
    # key the table does not name is passed through rather than dropped.
    out = normalize_change_keys([{"blackboard": _BB, "talentIndex": 3, "description": "x"}])
    assert list(out[0]) == ["blackboard", "talent_index", "description"]  # type: ignore[index]


def test_normalize_passes_through_non_list_and_non_dict_entries() -> None:
    assert normalize_change_keys(None) is None
    assert normalize_change_keys(42) == 42
    assert normalize_change_keys(["raw", 7]) == ["raw", 7]


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
