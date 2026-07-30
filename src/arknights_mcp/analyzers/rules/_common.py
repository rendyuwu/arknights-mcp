"""Shared building blocks for the deterministic threat rules (§V37 DRY; §V6, §V26).

One home for the pieces every rule reuses so no loop or constant is copy-pasted
across the rule modules (§V37):

* :func:`count_evidence` -- the enemy's ``total_count`` evidence ROW (§V101).
* :func:`distinct_refs` -- the §V35 distinct-``ref`` tally (count entities, not
  occurrence rows: an enemy seen at several level variants counts once).
* :func:`by_game_id` -- deterministic enemy iteration order (§V26).
* :func:`fuller_view_note` -- the §V108 routing sentence a deliberately coarse
  observation owes.
* the motion constant sets -- the shared typed-field vocabulary the aerial rule
  partitions on.

The ability-token machinery that used to live here (``ability_tokens``,
``is_aerial``, ``AbilityTokenRule``) is gone with the rules it served: no upstream
source carries a typed ability vocabulary at all, so the aura, crowd-control and
block-bypass rules were retired in §T210 (c) rather than re-grounded on a guess
(B160 (c); ADR 0016). See :data:`~arknights_mcp.analyzers.rules.RETIRED_RULES`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from arknights_mcp.analyzers.base import EnemyOccurrence, EvidenceItem

#: ``motion_type`` values (uppercased) that mean the enemy flies (authoritative).
FLY_MOTIONS = frozenset({"FLY", "FLYING", "AIR"})
#: ``motion_type`` values (uppercased) that mean the enemy is ground-bound.
GROUND_MOTIONS = frozenset({"WALK", "GROUND", "CRAWL", "CLIMB", "DRIFT", "SWIM", "WALL"})

#: The tool holding the fine view of every stage detail a threat rule summarises.
_FULLER_VIEW_TOOL = "get_stage"


def fuller_view_note(*, this_view: str, flag: str, fuller: str) -> str:
    """The §V108 routing sentence a deliberately coarse observation owes (B153).

    An analyzer works from a summary of the stage -- a route-RECORD count, tile
    tallies, aggregated spawn bounds -- while the per-record detail lives on
    ``get_stage`` behind an ``include_`` flag. Stating only what this view lacks
    ("geometry not clustered") reads as a claim about the SERVER: the reporting
    client concluded the map data did not exist and offered a community wiki, the
    one source class this project refuses to use. So a coarse limitation says what
    THIS view omits *and* names the tool + flag that returns the fine one.

    One §V37 home because three rules owe the same sentence about three different
    sections; written once, the three cannot drift into three shapes of one promise,
    and the guard that checks the named flag is a real ``get_stage`` input has a
    single place to check.

    ``this_view`` is what the analysis does not do, ``fuller`` what the flag returns.
    Client-facing text, so no internal cites or jargon (§V71 b) -- they live here.
    """
    return f"{this_view} Call {_FULLER_VIEW_TOOL} with {flag} for this stage to get {fuller}."


def count_evidence(occ: EnemyOccurrence) -> EvidenceItem | None:
    """The enemy's spawn-count evidence row, or ``None`` when the count is absent.

    §V101/B137: this used to be a ``note="total_count=43"`` string riding the deciding
    row, which put two numbers in one row -- one typed, the other buried in prose the
    client had to parse. ``total_count`` is a fact with its own emitted field path
    (``occurrences[].total_count``), so it is its own row (one fact per row). One §V37
    home: every rule that reports how many of an enemy a stage fields calls this, so
    the packed-note shape cannot come back one rule at a time.
    """
    if occ.total_count is None:
        return None
    return EvidenceItem(ref=occ.game_id, field="total_count", value=occ.total_count)


def distinct_refs(evidence: Sequence[EvidenceItem]) -> int:
    """Number of *distinct* evidence ``ref``s (§V35).

    An enemy that appears at several level variants yields several evidence items
    sharing one ``ref``; the headline counts distinct enemies, not evidence rows,
    so a single enemy is never reported as multiple types (B14).
    """
    return len({e.ref for e in evidence})


def by_game_id(occurrences: Iterable[EnemyOccurrence]) -> list[EnemyOccurrence]:
    """Occurrences sorted by ``game_id`` for deterministic evidence order (§V26)."""
    return sorted(occurrences, key=lambda o: o.game_id)
