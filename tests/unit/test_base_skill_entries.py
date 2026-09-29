"""``base_skill_entries``: which base-skill stage is in effect for an account (ADR 0021).

Bellone-shaped rows: slot 1 has stage α (E0 L1) replaced by β (E2 L1); slot 2 has one
stage (E0 L1).
"""

from __future__ import annotations

from arknights_mcp.db.repositories.operators import BaseSkillRow
from arknights_mcp.services.base_skills import base_skill_entries

_ROWS = (
    BaseSkillRow(1, 1, "Famiglia Business α", "TRADING", "alpha", 0, 1),
    BaseSkillRow(1, 2, "Famiglia Business β", "TRADING", "beta", 2, 1),
    BaseSkillRow(2, 1, "Outstanding Debt", "TRADING", "debt", 0, 1),
)


def _effect(entries: list[dict[str, object]]) -> list[tuple[object, object]]:
    return [(e["display_name"], e["in_effect"]) for e in entries]


def test_before_the_upgrade_the_first_stage_is_in_effect_and_the_next_is_locked() -> None:
    entries = base_skill_entries(_ROWS, progress=(1, 50))
    assert _effect(entries) == [
        ("Famiglia Business α", True),
        ("Famiglia Business β", False),
        ("Outstanding Debt", True),
    ]


def test_a_replaced_stage_is_dropped_once_its_successor_unlocks() -> None:
    entries = base_skill_entries(_ROWS, progress=(2, 1))
    assert _effect(entries) == [("Famiglia Business β", True), ("Outstanding Debt", True)]


def test_the_static_view_emits_every_stage_without_in_effect() -> None:
    entries = base_skill_entries(_ROWS)
    assert [e["display_name"] for e in entries] == [r.display_name for r in _ROWS]
    assert all("in_effect" not in e for e in entries)
    assert all(e["room_type"] == "TRADING" for e in entries)


def test_a_room_filter_keeps_that_room_and_drops_the_room_key() -> None:
    mixed = (*_ROWS, BaseSkillRow(3, 1, "Elsewhere", "POWER", None, 0, 1))
    entries = base_skill_entries(mixed, room_type="TRADING")
    assert len(entries) == 3
    assert all("room_type" not in e for e in entries)
