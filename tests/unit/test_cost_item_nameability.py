"""Shared cost-item nameability predicate (§T132/§V69; §V37 single home).

The upgrade-cost name pairing has three collaborators that MUST agree on which entries
are candidates for a display name: :func:`cost_item_ids` (what to look up),
:func:`pair_cost_item_names` (what to pair), and
:func:`~arknights_mcp.mcp.tools._shared.has_unnamed_cost_item` (what to flag as un-named).
All three route through one predicate, :func:`cost_item_id`, so they can never diverge --
an entry whose ``id`` is not a non-empty **string** is uniformly *not* nameable: never
looked up, never paired, and therefore never flagged as un-named. These pin that
alignment directly (the F3 regression: a non-string truthy id used to be flagged though
it was never pairable).
"""

from __future__ import annotations

from arknights_mcp.mcp.tools._shared import has_unnamed_cost_item
from arknights_mcp.services.operators import cost_item_id, cost_item_ids, pair_cost_item_names


def test_int_id_is_not_nameable_and_never_flagged() -> None:
    # A non-string (int) id is not the game-data cost shape: the pairer skips it, so the
    # detector must skip it too (the two sides share one predicate).
    cost = [{"id": 30034, "count": 4}]
    assert cost_item_id(cost[0]) is None
    assert cost_item_ids(cost) == set()
    assert pair_cost_item_names(cost, {}) == cost  # never touched
    assert has_unnamed_cost_item([cost]) is False  # ... so never flagged


def test_str_id_without_name_is_nameable_and_flagged() -> None:
    # A non-empty string id IS a pairing candidate; with no name in the build it stays a
    # bare id (never fabricated, §V26) and the detector flags the un-named entry (§V69).
    cost = [{"id": "mat_x", "count": 4, "type": "MATERIAL"}]
    assert cost_item_id(cost[0]) == "mat_x"
    assert cost_item_ids(cost) == {"mat_x"}
    assert pair_cost_item_names(cost, {}) == cost  # name absent -> left as stored
    assert has_unnamed_cost_item([cost]) is True


def test_resolved_name_is_not_flagged() -> None:
    # Once pairing resolves a name, the entry carries display_name -> not flagged.
    cost = [{"id": "mat_x", "count": 4, "type": "MATERIAL"}]
    paired = pair_cost_item_names(cost, {"mat_x": "Orirock Cube"})
    assert paired == [
        {"id": "mat_x", "count": 4, "type": "MATERIAL", "display_name": "Orirock Cube"}
    ]
    assert has_unnamed_cost_item([paired]) is False


def test_empty_or_missing_id_is_not_nameable() -> None:
    assert cost_item_id({"id": "", "count": 1}) is None
    assert cost_item_id({"count": 1}) is None
    assert has_unnamed_cost_item([[{"id": "", "count": 1}, {"count": 1}]]) is False


def test_non_list_and_non_dict_entries_contribute_nothing() -> None:
    assert cost_item_ids(None) == set()
    assert pair_cost_item_names(None, {}) is None
    # a non-list cost and a non-dict entry are both inert for the detector.
    assert has_unnamed_cost_item([None, "x", 5, [42, "y"]]) is False
