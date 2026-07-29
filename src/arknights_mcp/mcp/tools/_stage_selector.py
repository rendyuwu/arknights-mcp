"""§V102 selector disclosure for the three stage tools (§T195, B139; §V37 one home).

``get_stage`` / ``analyze_stage`` / ``get_stage_drops`` share one selector -- region plus
exactly one of ``stage_code`` | ``game_id`` -- and one problem with it: a stage_code is not
unique, so the lookup silently answered with one of several stages (B139). The typed half
of the disclosure (which stage was picked, what the alternates are) is produced once in
:mod:`arknights_mcp.services.stages`; the client-facing WORDING lives here, in one home,
because it is read by two tool modules and must not drift between them (§V37).

Kept out of :mod:`arknights_mcp.mcp.tools._shared` because that module is at its §V38
line budget and this is a distinct responsibility group: one pre-call description note,
one post-call limitation, one suggested-action extension, and the economy cap they share.
Client-facing text throughout, so no internal cites/jargon reaches a string (§V71 b) --
the cites live in the comments.
"""

from __future__ import annotations

from arknights_mcp.services.stages import StageAmbiguity

#: §V102 (a) (§T195, B139): the selector CONTRACT of the three stage tools (``get_stage``
#: / ``analyze_stage`` / ``get_stage_drops``), stated PRE-call in each description because
#: it changes how a caller forms the call (§V111 c). Two facts, both previously learnable
#: only the hard way: "exactly one of stage_code | game_id" appeared in NO description or
#: schema, so a client discovered it by tripping an ``invalid_input``; and a stage_code is
#: NOT unique -- 927 en codes (2293 stages) on the 2026-07-28 build are shared, and the
#: lookup silently answered with one of them (B139). The deterministic pick is stated here;
#: WHICH stage answered and what the alternates are is a per-response fact, so it rides the
#: response as :func:`stage_ambiguity_limitation` (§V102 b, §V111 a: the limitation home).
#: Shared: one wording, one home (§V37). Client-facing text, so no internal cites/jargon
#: (§V71 b); short sentences (§V71 f).
STAGE_SELECTOR_NOTE = (
    "Give exactly one of stage_code or game_id. Several stages can share one stage_code. "
    "The first is returned, and a limitation then names it and the alternates' game_ids."
)


#: §V22/§V66 economy cap on the alternates NAMED in that limitation. Counted on the
#: 2026-07-28 build rather than guessed (§V96): en shared-code groups are 775 of size 2,
#: 134 of size 3, 5 of size 4, then a tail to 36 (``LT-1``..``LT-6``) -- so 8 names every
#: alternate for 914 of 927 groups, and the rare tower group is summarised ("and N more")
#: instead of putting 35 ids in one sentence. The count itself is always exact, so a
#: shortened list never under-states how ambiguous the code was.
MAX_LISTED_ALTERNATES = 8


def _alternates_phrase(ambiguity: StageAmbiguity) -> str:
    """The alternates' game_ids as one bounded phrase (§V22/§V66).

    Names up to :data:`MAX_LISTED_ALTERNATES` ids, then summarises the remainder; a
    matching set that hit the service's own read cap ends open ("and others") rather
    than implying the list is complete (§V26)."""
    shown = list(ambiguity.alternates[:MAX_LISTED_ALTERNATES])
    listed = ", ".join(shown)
    if ambiguity.truncated:
        return f"{listed}, and others"
    remaining = len(ambiguity.alternates) - len(shown)
    return f"{listed}, and {remaining} more" if remaining else listed


def stage_ambiguity_limitation(ambiguity: StageAmbiguity | None) -> tuple[str, ...]:
    """§V102 (b) (§T195, B139): disclose which stage a shared ``stage_code`` resolved to.

    A stage_code selects one stage but names several: ``get_stage(stage_code="4-4")``
    answered with ``main_04-04`` while ``search_stages("4-4")`` showed that AND
    ``main_04-04#f#``, with no note, no limitation, and no way to reach the four-star
    variant except a game_id nothing pointed at -- so the client believed it had asked
    about "4-4" and been told about "4-4". This names the stage that answered, its
    §V80-truthful ``difficulty``, and the alternates' game_ids, which are the only handle
    that selects one of them; it points at ``search_stages``, an MCP-callable tool, never
    a CLI command (§V71 a).

    Returns an empty tuple when the selector was unambiguous (a game_id lookup, or a code
    matching one stage), so an unambiguous response carries no noise. Shared §V37 home for
    all three stage tools. Client-facing text, so no internal cites/jargon (§V71 b)."""
    if ambiguity is None:
        return ()
    total = len(ambiguity.alternates) + 1
    count = f"at least {total}" if ambiguity.truncated else str(total)
    chosen_difficulty = ambiguity.chosen_difficulty
    difficulty = f", difficulty {chosen_difficulty}" if chosen_difficulty else ""
    return (
        f"The stage_code {ambiguity.stage_code} is shared by {count} stages in this "
        f"region. This response is {ambiguity.chosen_game_id}{difficulty}, the first of "
        f"them. The others are {_alternates_phrase(ambiguity)}. Pass one of those "
        "game_ids as game_id to get that stage, or use search_stages to list them.",
    )


def stage_ambiguity_drop_hint(ambiguity: StageAmbiguity | None) -> tuple[str, ...]:
    """Name the alternates that may hold the drops a shared code hid (§V102/§V24; T195).

    ``get_stage_drops`` can answer "no drops" for a stage that resolved fine, and a shared
    stage_code makes that answer an artefact of the PICK rather than of the data -- on the
    2026-07-28 build 206 shared codes have a first-by-order stage with no drops while a
    sibling under the SAME code has them (cn "10-10" picks ``easy_10-09`` over
    ``main_10-09`` / ``tough_10-09``). Naming the alternates turns a dead end into the one
    retry that works.

    This text was T195's ``not_found`` ``suggested_action``. §V106 (b) made that answer an
    ``ok`` (the stage resolved; only the drop SET is empty), which leaves an ``ok``
    envelope with no ``suggested_action`` field to carry it -- so it MOVED to the
    limitation surface rather than being dropped (§V111 b). It is deliberately separate
    from :func:`stage_ambiguity_limitation`, which discloses the pick on EVERY shared-code
    response; this one adds the drops-specific retry and rides only the empty answer.

    Returns an empty tuple when the selector was unambiguous. Client-facing text (§V71 b).
    """
    if ambiguity is None:
        return ()
    return (
        f"The stage_code {ambiguity.stage_code} is also used by "
        f"{_alternates_phrase(ambiguity)}, which may be the stage that holds the drop "
        "data -- retry with one of those game_ids.",
    )
