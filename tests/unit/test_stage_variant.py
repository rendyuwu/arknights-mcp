"""§V80/§T164 stage-variant derivation tests (B84).

:func:`stage_variant` is the ONE §V37 home both ``get_stage`` and the search
locators route through, so these pin the pure logic: a ``tough_*`` / ``easy_*``
game_id is upgraded off ``NORMAL`` to its truthful variant tag, a genuine source
variant (``FOUR_STAR``) is never clobbered, and every non-variant / non-stage id
passes its source difficulty through unchanged.
"""

from __future__ import annotations

import pytest

from arknights_mcp.services.stage_variant import stage_variant


@pytest.mark.parametrize(
    ("game_id", "difficulty", "expected"),
    [
        # §V80/B84: tough/easy prefix on a source-NORMAL (or unset) stage -> variant tag.
        ("tough_14-06", "NORMAL", "TOUGH"),
        ("tough_14-06", None, "TOUGH"),
        ("easy_14-06", "NORMAL", "EASY"),
        ("easy_14-06", None, "EASY"),
        # A plain main-story stage keeps its source difficulty (NORMAL / None).
        ("main_04-04", "NORMAL", "NORMAL"),
        ("main_04-04", None, None),
        # The source FOUR_STAR challenge variant (#f# suffix, no tough/easy prefix)
        # passes through unchanged -- it is already distinguishable (§V70).
        ("main_04-04#f#", "FOUR_STAR", "FOUR_STAR"),
        # A more-specific source variant is authoritative even under a tough/easy
        # prefix: the prefix rule never clobbers a real FOUR_STAR.
        ("tough_04-04#f#", "FOUR_STAR", "FOUR_STAR"),
        # Non-stage ids (operator / enemy / item) never match a prefix -> untouched.
        ("char_002_amiya", None, None),
        ("enemy_1000_gp_c", None, None),
        ("30012", None, None),
    ],
)
def test_stage_variant(game_id: str, difficulty: str | None, expected: str | None) -> None:
    assert stage_variant(game_id, difficulty) == expected


def test_tough_and_easy_are_never_normal() -> None:
    # §V80 headline: a tough_*/easy_* game_id is NEVER emitted as NORMAL.
    assert stage_variant("tough_14-06", "NORMAL") != "NORMAL"
    assert stage_variant("easy_14-06", "NORMAL") != "NORMAL"
