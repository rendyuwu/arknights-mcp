"""Shared building blocks for the deterministic threat rules.

One home for the pieces every rule reuses so no loop or constant is copy-pasted
across the rule modules:

* :func:`count_evidence` -- the enemy's ``total_count`` evidence ROW.
* :func:`declined` -- the no-conclusion result that KEEPS its refusals.
* :func:`distinct_refs` -- the distinct-``ref`` tally (count entities, not
  occurrence rows: an enemy seen at several level variants counts once).
* :func:`by_game_id` -- deterministic enemy iteration order.
* :func:`fuller_view_note` -- the routing sentence a deliberately coarse
  observation owes.
* the motion constant sets -- the shared typed-field vocabulary the aerial rule
  partitions on.

The ability-token machinery that used to live here (``ability_tokens``,
``is_aerial``, ``AbilityTokenRule``) is gone with the rules it served: no upstream
source carries a typed ability vocabulary at all, so the aura, crowd-control and
block-bypass rules were retired rather than re-grounded on a guess
(ADR 0016). See :data:`~arknights_mcp.analyzers.rules.RETIRED_RULES`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from arknights_mcp.analyzers.base import EnemyOccurrence, EvidenceItem, RuleResult

#: ``motion_type`` values (uppercased) that mean the enemy flies (authoritative), and
#: the ones that mean it is ground-bound. This is the COUNTED domain, not a guessed
#: vocabulary.
#:
#: These two sets used to carry ten tokens between them -- ``FLYING``/``AIR`` and
#: ``GROUND``/``CRAWL``/``CLIMB``/``DRIFT``/``SWIM``/``WALL`` -- over a real domain of
#: two: the promoted build stores WALK on 26526 stage-enemy occurrences and FLY on
#: 1776, and nothing else, on either server. The eight extra members were not inert
#: padding. A rule that partitions on a guessed token ANSWERS the case its own
#: "unrecognized motion_type" arm exists to refuse: a new ground-ish token upstream
#: would have been swallowed as ground with no limitation at all, and a new fly-ish one
#: would have produced an aerial conclusion at 0.9 confidence -- both from semantics no
#: source has ever stated. That is also what left the arm with zero fires over
#: 1030 observations, so the fat vocabulary and the dead guard are one defect.
#:
#: So the vocabulary is exactly what the corpus sends, and the refusal arm handles
#: everything else -- which is what makes it a tripwire rather than decoration.
FLY_MOTIONS = frozenset({"FLY"})
GROUND_MOTIONS = frozenset({"WALK"})

#: The tool holding the fine view of every stage detail a threat rule summarises.
_FULLER_VIEW_TOOL = "get_stage"


def fuller_view_note(*, this_view: str, flag: str, fuller: str) -> str:
    """The routing sentence a deliberately coarse observation owes.

    An analyzer works from a summary of the stage -- a route-RECORD count, tile
    tallies, aggregated spawn bounds -- while the per-record detail lives on
    ``get_stage`` behind an ``include_`` flag. Stating only what this view lacks
    ("geometry not clustered") reads as a claim about the SERVER: the reporting
    client concluded the map data did not exist and offered a community wiki, the
    one source class this project refuses to use. So a coarse limitation says what
    THIS view omits *and* names the tool + flag that returns the fine one.

    One home because three rules owe the same sentence about three different
    sections; written once, the three cannot drift into three shapes of one promise,
    and the guard that checks the named flag is a real ``get_stage`` input has a
    single place to check.

    ``this_view`` is what the analysis does not do, ``fuller`` what the flag returns.
    Client-facing text, so no internal cites or jargon -- they live here.
    """
    return f"{this_view} Call {_FULLER_VIEW_TOOL} with {flag} for this stage to get {fuller}."


def declined(
    limitations: Sequence[str],
    warnings: Sequence[str] = (),
) -> RuleResult:
    """The result of a rule that concluded nothing, with its refusals kept.

    Every rule ends in ``if not evidence: return RuleResult()``, and that line used to
    throw away every limitation the pass had accumulated. The refusal then reached a
    client only when some OTHER enemy in the same stage happened to produce a
    conclusion to carry it -- ``def_res_skew`` alone dropped 80 res-missing rows over 78
    stages that way. The narrowing was declared and left standing, which
    made the declaration true and the client no wiser.

    So a refusal that has no observation to ride rides ``warnings``: the warning channel
    that survives without one (``services/drops.py`` carries its excluded stages the
    same way -- "no observation to subsume it"). Same text, different carrier, so the
    marker still identifies the arm in either channel.

    One home because four rules owe the same handling; written once, a fifth rule
    cannot quietly reinstate the discard.
    """
    return RuleResult(warnings=(*warnings, *limitations))


def count_evidence(occ: EnemyOccurrence) -> EvidenceItem | None:
    """The enemy's spawn-count evidence row, or ``None`` when the count is absent.

    This used to be a ``note="total_count=43"`` string riding the deciding
    row, which put two numbers in one row -- one typed, the other buried in prose the
    client had to parse. ``total_count`` is a fact with its own emitted field path
    (``occurrences[].total_count``), so it is its own row (one fact per row). One
    home: every rule that reports how many of an enemy a stage fields calls this, so
    the packed-note shape cannot come back one rule at a time.
    """
    if occ.total_count is None:
        return None
    return EvidenceItem(ref=occ.game_id, field="total_count", value=occ.total_count)


def distinct_refs(evidence: Sequence[EvidenceItem]) -> int:
    """Number of *distinct* evidence ``ref``s.

    An enemy that appears at several level variants yields several evidence items
    sharing one ``ref``; the headline counts distinct enemies, not evidence rows,
    so a single enemy is never reported as multiple types.
    """
    return len({e.ref for e in evidence})


def by_game_id(occurrences: Iterable[EnemyOccurrence]) -> list[EnemyOccurrence]:
    """Occurrences sorted by ``game_id`` for deterministic evidence order."""
    return sorted(occurrences, key=lambda o: o.game_id)
