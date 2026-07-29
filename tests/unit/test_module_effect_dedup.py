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


# --- label_token_effects: rename the source's own isToken flag (§V83/§V115) ----


def test_source_token_flag_becomes_the_applies_to_label() -> None:
    # §V115/B162: the label reports the field that STATES the fact -- the part's own
    # isToken, carried down onto each bundle by the importer -- and the raw source key
    # never reaches the wire.
    out = label_token_effects([{"talentIndex": 1, "isToken": True, "blackboard": _BB}])
    assert out == [{"talentIndex": 1, "applies_to": "token", "blackboard": _BB}]


def test_source_operator_flag_is_an_answer_not_a_silence() -> None:
    # isToken false is a STATEMENT ("this describes the operator"), so it ships as a
    # label rather than as an absent key -- absence is reserved for a source that said
    # nothing (§V67/§V114).
    out = label_token_effects([{"talentIndex": 1, "isToken": False, "blackboard": _BB}])
    assert out == [{"talentIndex": 1, "applies_to": "operator", "blackboard": _BB}]


def test_talent_index_minus_one_alone_earns_no_label() -> None:
    # The B162 regression in one assertion: -1 is a change with no talent index of its
    # own, NOT a token marker -- 454 of 513 en rows carrying it sit on parts the source
    # flags isToken false. Unflagged, it gets no applies_to at all.
    row = {"talentIndex": -1, "blackboard": _BB}
    assert label_token_effects([row]) == [row]


def test_minus_one_on_an_operator_part_is_labelled_operator() -> None:
    # The same sentinel WITH the source's flag: the label follows the flag, not the -1.
    out = label_token_effects([{"talentIndex": -1, "isToken": False, "blackboard": _BB}])
    assert out == [{"talentIndex": -1, "applies_to": "operator", "blackboard": _BB}]


def test_label_is_idempotent_and_passes_through_non_list() -> None:
    already = {"talentIndex": -1, "applies_to": "token"}
    assert label_token_effects([already]) == [already]
    assert label_token_effects(None) is None


def test_dedup_and_label_composes_all_three_steps() -> None:
    # The service pipeline: collapse duplicates, label from the source flag, THEN rename
    # the keys for the wire (§V71 d). The rename is last so the first two steps still read
    # the source's own names.
    row = {
        "talentIndex": -1,
        "requiredPotentialRank": 0,
        "unlockCondition": _COND,
        "blackboard": _BB,
        "isToken": True,
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


def test_two_povs_of_one_change_stay_two_rows() -> None:
    # §V83 amended (B162): 89 en / 101 cn groups carry both an operator-POV and a
    # token-POV copy under one (talentIndex, requiredPotentialRank). isToken is part of
    # the identity, so they never merge.
    operator_pov = {
        "talentIndex": 1,
        "requiredPotentialRank": 0,
        "unlockCondition": _COND,
        "blackboard": _BB,
        "isToken": False,
    }
    token_pov = {**operator_pov, "isToken": True}
    out = dedup_and_label_changes([operator_pov, token_pov])
    assert [row["applies_to"] for row in out] == ["operator", "token"]  # type: ignore[index]


def test_an_unmarked_change_never_absorbs_a_token_label() -> None:
    # This is what putting isToken IN the §V83 identity actually buys, measured rather
    # than assumed: a first attempt at this guard used two fully-populated POVs and passed
    # with the member removed, because a differing isToken is also a value CONFLICT and
    # the conflict check alone keeps that pair apart.
    #
    # The case only the identity decides is a bundle the source never marked meeting one
    # it did. Absent is not a conflict -- it is subsumed -- so without the member the two
    # merge and the unmarked bundle comes out labelled "token": a statement the source
    # never made, which is B162's failure mode in miniature (§V115 c/§V67).
    unmarked = {
        "talentIndex": 1,
        "requiredPotentialRank": 0,
        "unlockCondition": _COND,
        "blackboard": _BB,
    }
    token_pov = {
        "talentIndex": 1,
        "requiredPotentialRank": 0,
        "unlockCondition": _COND,
        "isToken": True,
    }
    out = dedup_and_label_changes([unmarked, token_pov])
    assert len(out) == 2, "an unmarked bundle was merged into a marked one"
    assert "applies_to" not in out[0]  # type: ignore[operator]
    assert out[1]["applies_to"] == "token"  # type: ignore[index]


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
