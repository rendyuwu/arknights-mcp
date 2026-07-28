"""T196: the two payload rules at their single home (§V67 null, §V103 source mask).

The contract guards (``tests/contract/test_envelope_null_discipline.py`` and
``test_source_placeholder_disclosure.py``) assert the rules hold over a corpus. These
assert the rules themselves: what the mask predicate does at its boundaries, and that the
null sweep reaches every shape a payload can take rather than only the shapes today's
tools happen to build.
"""

from __future__ import annotations

import pytest

from arknights_mcp.mcp.envelopes import build_envelope, ok
from arknights_mcp.mcp.payload_hygiene import clean_payload, is_source_mask, mask_limitation

# --- §V103: the mask predicate -------------------------------------------------


#: Every distinct mask token COUNTED on the promoted build (§V96). Kept here as well as in
#: the contract guard so the predicate still regresses loudly in CI, where no build exists.
_REAL_MASKS = ("？？？", "？？？？？", "-", "???", "??:??:??")


@pytest.mark.parametrize("value", _REAL_MASKS)
def test_the_real_corpus_masks_are_detected(value: str) -> None:
    assert is_source_mask(value)


@pytest.mark.parametrize("value", ["", "   ", "\t", "???", "…", "＊＊＊", "--", "?!"])
def test_empty_and_unseen_mask_shapes_are_detected(value: str) -> None:
    """§V103 names "empty-after-sanitize" as a mask, and §V96 forbids assuming the domain
    is closed: ``＊＊＊`` has never appeared in this corpus, and the predicate still catches
    it because it keys on shape rather than on a list of literals someone maintained."""
    assert is_source_mask(value)


@pytest.mark.parametrize(
    "value",
    [
        "Amiya",  # a Latin name
        "阿米娅",  # the same name in CN -- \w is Unicode-aware, so this is NOT a mask
        "オペレーター",  # kana, same reason
        "4-4",  # a stage code that merely contains a dash
        "?-?-1",  # mostly mask characters, but it carries a digit
        "E2",
        "0",
    ],
)
def test_real_content_is_never_flagged(value: str) -> None:
    assert not is_source_mask(value)


# --- §V103: scope + disclosure -------------------------------------------------


def test_only_name_like_keys_are_scanned() -> None:
    """The server's OWN rendering alphabet is not a source mask.

    ``tile_grid.absent_symbol`` is ``"."`` and the tile legend's symbols are single
    characters -- the predicate matches all of them. Disclosing those as source
    placeholders would be a false claim about upstream, and a limitation a client cannot
    act on teaches it to skim past the ones that matter.
    """
    payload = {
        "tile_grid": {"absent_symbol": ".", "rows": ["##.", ".#."], "legend": [{"symbol": "#"}]},
        "stage": {"stage_code": "???"},
    }
    _, limitations = clean_payload(payload)
    assert len(limitations) == 1
    assert "stage.stage_code" in limitations[0]
    assert "absent_symbol" not in limitations[0]


@pytest.mark.parametrize(
    "key",
    ["display_name", "zone_display_name", "event_name", "description", "title", "stage_code"],
)
def test_every_name_like_wire_key_is_in_scope(key: str) -> None:
    _, limitations = clean_payload({key: "？？？"})
    assert limitations and key in limitations[0]


def test_the_masked_value_is_still_emitted() -> None:
    """§V26: the fix is disclosure, not substitution.

    Dropping or replacing the value would be the server inventing a name, which is the
    failure this rule exists to prevent -- ``？？？`` IS what the source says.
    """
    payload, limitations = clean_payload({"display_name": "？？？"})
    assert payload == {"display_name": "？？？"}
    assert limitations


def test_the_disclosure_is_bounded_with_an_exact_remainder() -> None:
    """§V22/§V66: a pathological payload gets a bounded string, not one path per mask."""
    masks = [(f"rows[{index}].display_name", "-") for index in range(20)]
    text = mask_limitation(masks)
    assert "rows[7].display_name" in text
    assert "rows[8].display_name" not in text
    assert "and 12 more field(s) like it" in text


def test_the_disclosure_carries_no_internal_cite() -> None:
    # §V71 (b): client-facing runtime text never ships a spec cite or internal jargon.
    text = mask_limitation([("stage.display_name", "??:??:??")])
    assert "§" not in text
    assert "B141" not in text and "V103" not in text


def test_a_clean_payload_earns_no_limitation() -> None:
    _, limitations = clean_payload({"display_name": "Amiya", "stage_code": "4-4"})
    assert limitations == ()


# --- §V67: the null sweep ------------------------------------------------------


def test_nulls_are_dropped_at_every_depth() -> None:
    """The five B135 sites are a top-level key, a nested-object key, and two list
    elements two levels down; the sweep has to reach all of them in one pass."""
    payload, _ = clean_payload(
        {
            "top": None,
            "map": {"width": 8, "map_version": None},
            "talents": [{"display_name": None, "variants": [{"blackboard": None, "index": 0}]}],
            "levels": [{"level": 1, "talent_changes": None}],
        }
    )
    assert payload == {
        "map": {"width": 8},
        "talents": [{"variants": [{"index": 0}]}],
        "levels": [{"level": 1}],
    }


def test_confirmed_none_survives_the_sweep() -> None:
    """§V67's whole distinction: ``[]`` means the source CONFIRMS none and must reach the
    client intact. A sweep that also collapsed empty collections would erase the very
    signal the null ban exists to make readable."""
    payload, _ = clean_payload({"drops": [], "blackboard": {}, "count": 0, "flag": False})
    assert payload == {"drops": [], "blackboard": {}, "count": 0, "flag": False}


def test_a_null_list_element_is_left_visible() -> None:
    """Dropping it would shift every later index and silently break a positional join, so
    it survives for the contract guard to fail on rather than being quietly repaired."""
    payload, _ = clean_payload({"rows": [1, None, 3]})
    assert payload == {"rows": [1, None, 3]}


def test_the_envelope_applies_both_rules() -> None:
    """Both rules ride the single builder every tool passes through, so a tool gets them
    without opting in -- the property B135's four per-surface rollouts lacked."""
    envelope = ok({"display_name": "？？？", "missing": None, "kept": 1})
    body = envelope.to_dict()
    assert body["data"] == {"display_name": "？？？", "kept": 1}
    assert any(text.startswith("placeholder text from the source") for text in body["limitations"])


def test_the_analyzer_version_key_is_absent_when_there_is_no_analyzer() -> None:
    # §V67 (B135): the one null that lives on the envelope itself rather than in ``data``.
    assert "analyzer_version" not in ok({"a": 1}).to_dict()
    assert (
        build_envelope("ok", data={"a": 1}, analyzer_version="3").to_dict()["analyzer_version"]
        == "3"
    )


def test_a_disclosure_does_not_displace_the_tool_own_limitations() -> None:
    envelope = build_envelope("ok", data={"display_name": "-"}, limitations=("existing caveat",))
    assert envelope.limitations[0] == "existing caveat"
    assert len(envelope.limitations) == 2
