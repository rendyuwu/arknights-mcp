"""Single home (§V37) for the stage-variant tag derivation (§V70/§V80/§T164).

The wire ``difficulty`` field is the client-facing stage VARIANT tag, not the raw
``stages.difficulty`` column read blind. Arknights carries the challenge / emergency
variant in the source ``difficulty`` field itself (``FOUR_STAR``, game_id suffix
``#f#`` -- already distinguishable, §V70) but leaves the story-mode and adverse-mode
variants (game_id prefixes ``easy_`` / ``tough_``) on ``NORMAL``. So a
``tough_14-06`` stage reports source ``difficulty:"NORMAL"`` even though it is the
tough variant of ``14-06`` (B84): two stages sharing a ``display_name`` +
``stage_code`` then differ only by the undocumented game_id prefix.

:func:`stage_variant` upgrades that prefix-derived variant so the emitted tag is
truthful (§V80: a ``tough_*`` / ``easy_*`` game_id is never labelled ``NORMAL``),
while never clobbering an already-specific source variant (a real ``FOUR_STAR``
survives). It is the ONE home both ``get_stage``
(:mod:`arknights_mcp.services.stages`) and the search locators
(:mod:`arknights_mcp.services.search`) route through, so every surface emits the
same tag for the same stage (§V80: get_stage + both search locators agree).
"""

from __future__ import annotations

#: game_id prefix -> variant tag. A stage whose game_id starts with one of these is
#: that variant even when its source ``difficulty`` column stayed ``NORMAL`` (B84).
_PREFIX_VARIANTS: tuple[tuple[str, str], ...] = (
    ("tough_", "TOUGH"),
    ("easy_", "EASY"),
)

#: Source difficulty values a prefix variant may override. A more-specific source
#: variant (e.g. ``FOUR_STAR``) is authoritative and is never overwritten.
_OVERRIDABLE: frozenset[str | None] = frozenset({None, "NORMAL"})


def stage_variant(game_id: str, difficulty: str | None) -> str | None:
    """Derive the truthful stage-variant tag from ``game_id`` + source ``difficulty``.

    Returns the source ``difficulty`` unchanged for a plain stage (``NORMAL`` /
    ``FOUR_STAR`` / ``None``) and for any non-stage id (no prefix matches, so an
    operator / enemy / item locator is untouched). A ``tough_*`` / ``easy_*``
    game_id whose source difficulty is unset or ``NORMAL`` is upgraded to ``TOUGH``
    / ``EASY`` (§V80/B84); an already-specific source variant (``FOUR_STAR``) is
    never clobbered.
    """
    for prefix, variant in _PREFIX_VARIANTS:
        if game_id.startswith(prefix):
            return variant if difficulty in _OVERRIDABLE else difficulty
    return difficulty
