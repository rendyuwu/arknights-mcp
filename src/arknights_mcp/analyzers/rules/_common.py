"""Shared building blocks for the deterministic threat rules (§V37 DRY; §V6, §V26).

One home for the pieces every rule reuses so no loop or constant is copy-pasted
across the rule modules (§V37):

* :func:`count_note` -- the ``total_count=<n>`` evidence note.
* :func:`distinct_refs` -- the §V35 distinct-``ref`` tally (count entities, not
  occurrence rows: an enemy seen at several level variants counts once).
* :func:`by_game_id` -- deterministic enemy iteration order (§V26).
* :func:`ability_tokens` / :func:`is_aerial` and the motion constant sets --
  shared typed-field predicates (the aerial rule *and* the block-bypass rule both
  need "is this enemy a flyer?"; it lives here, not in two copies).
* :class:`AbilityTokenRule` -- the generic typed-ability-token matcher the aura
  and crowd-control rules are configured instances of; they differ only in their
  token set and wording, expressed as data (§V37), never as a duplicated loop.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from arknights_mcp.analyzers.base import (
    EnemyOccurrence,
    EvidenceItem,
    Observation,
    RuleResult,
    StageThreatContext,
)

#: ``abilities`` token (lowercased) that means the enemy flies.
AERIAL_ABILITY = "aerial"
#: ``motion_type`` values (uppercased) that mean the enemy flies (authoritative).
FLY_MOTIONS = frozenset({"FLY", "FLYING", "AIR"})
#: ``motion_type`` values (uppercased) that mean the enemy is ground-bound.
GROUND_MOTIONS = frozenset({"WALK", "GROUND", "CRAWL", "CLIMB", "DRIFT", "SWIM", "WALL"})


def count_note(count: int | None) -> str | None:
    """The ``total_count=<n>`` evidence note, or ``None`` when the count is absent."""
    return f"total_count={count}" if count is not None else None


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


def ability_tokens(abilities: tuple[str, ...] | None) -> set[str] | None:
    """Lowercased typed ability tokens, or ``None`` when the field is absent (§V26).

    ``None`` (field absent) and ``set()`` (present but empty) are kept distinct so a
    rule can tell "unknown" from "known to have no abilities".
    """
    return None if abilities is None else {a.lower() for a in abilities}


def is_aerial(occ: EnemyOccurrence) -> bool:
    """True iff a typed field marks the enemy as flying (§V26; motion or ability).

    Shared with the block-bypass rule, which excludes flyers because a flyer's
    unblockability is already reported by the aerial rule (avoids a redundant
    finding for the same enemy).
    """
    motion = occ.motion_type.upper() if occ.motion_type else None
    if motion in FLY_MOTIONS:
        return True
    tokens = ability_tokens(occ.abilities)
    return tokens is not None and AERIAL_ABILITY in tokens


@dataclass(frozen=True)
class AbilityTokenRule:
    """A typed-ability-token threat rule (§V6, §V26).

    Fires on every enemy whose typed ``abilities`` intersect ``tokens``; emits one
    observation whose headline is the distinct-enemy count (§V35). The aura and
    crowd-control rules are instances that differ only in ``tokens`` + wording --
    one matcher, no copied loop (§V37). Decides from typed tokens only, never from
    a name/description string (§V26).
    """

    rule_id: str
    category: str
    tag: str
    title: str
    tokens: frozenset[str]
    confidence: float
    #: ``{n}`` is substituted with the distinct-enemy count (§V35).
    summary_template: str

    def evaluate(self, ctx: StageThreatContext) -> RuleResult:
        evidence: list[EvidenceItem] = []
        for occ in by_game_id(ctx.occurrences):
            tokens = ability_tokens(occ.abilities)
            if not tokens:
                continue
            hit = sorted(tokens & self.tokens)
            if not hit:
                continue
            evidence.append(
                EvidenceItem(
                    ref=occ.game_id,
                    field="abilities",
                    value=hit[0],
                    note=count_note(occ.total_count),
                )
            )
        if not evidence:
            return RuleResult()
        return RuleResult(
            observation=Observation(
                rule_id=self.rule_id,
                category=self.category,
                tag=self.tag,
                title=self.title,
                summary=self.summary_template.format(n=distinct_refs(evidence)),
                confidence=self.confidence,
                evidence=tuple(evidence),
                limitations=(),
            )
        )
