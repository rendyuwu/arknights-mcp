"""Block-bypass threat rule (§V6, §V26): flags *ground* enemies that cannot be
stopped by blocking -- their typed ``block_behavior`` marks them unblockable, or a
typed ability lets them ignore/skip a blocker.

Flyers are excluded on purpose: a flyer's unblockability is already reported by the
aerial rule (:func:`~arknights_mcp.analyzers.rules._common.is_aerial`), so
reporting it again here would double-count the same enemy. Decides from typed
fields only (§V26): a ``block_behavior`` that states the enemy *is* blockable while
an ability claims bypass is a conflict -> omitted + warned (§V26); a missing
``block_behavior`` behind an ability inference reduces confidence + records a
limitation (§V26). One enemy across several level variants counts once (§V35).
"""

from __future__ import annotations

from typing import Any

from arknights_mcp.analyzers.base import (
    EvidenceItem,
    Observation,
    RuleResult,
    StageThreatContext,
)
from arknights_mcp.analyzers.rules._common import (
    ability_tokens,
    by_game_id,
    count_note,
    distinct_refs,
    is_aerial,
)

RULE_ID = "threat.block_bypass"

#: Typed ability tokens meaning the enemy ignores/skips a blocker.
_BYPASS_ABILITIES: frozenset[str] = frozenset(
    {"unblockable", "ignore_block", "block_bypass", "phase", "teleport", "burrow"}
)
#: Substrings in a lowercased ``block_behavior`` that mean "cannot be blocked".
_UNBLOCKABLE_MARKERS: tuple[str, ...] = ("unblockable", "ignore_block", "no_block", "bypass")

_CONF_BLOCK_FIELD = 0.85  # authoritative typed block_behavior
_CONF_ABILITY_ONLY = 0.6  # inferred from an ability when block_behavior is absent


def _summary(count: int) -> str:
    types_word = "type" if count == 1 else "types"
    return (
        f"Stage fields {count} ground enemy {types_word} that bypass or ignore blocking; "
        "blockers alone cannot stop their advance."
    )


class BlockBypassRule:
    """Flags ground enemies that cannot be held by a blocker (§V6, §V26)."""

    rule_id = RULE_ID

    def evaluate(self, ctx: StageThreatContext) -> RuleResult:
        evidence: list[EvidenceItem] = []
        limitations: list[str] = []
        warnings: list[str] = []
        confidence = 0.0

        for occ in by_game_id(ctx.occurrences):
            if is_aerial(occ):
                continue  # a flyer's unblockability is reported by the aerial rule
            bb = occ.block_behavior.lower() if occ.block_behavior else None
            tokens = ability_tokens(occ.abilities)
            has_bypass_ability = tokens is not None and bool(tokens & _BYPASS_ABILITIES)
            bb_unblockable = bb is not None and any(m in bb for m in _UNBLOCKABLE_MARKERS)

            deciding_field: str | None = None
            deciding_value: Any = None
            conf = 0.0

            if bb_unblockable:
                deciding_field = "block_behavior"
                deciding_value = occ.block_behavior
                conf = _CONF_BLOCK_FIELD
            elif has_bypass_ability:
                if bb is not None:
                    # block_behavior is stated and does NOT mark unblockable, yet an
                    # ability claims bypass -> conflicting typed fields (§V26): omit.
                    warnings.append(
                        f"{occ.game_id}: block_behavior={occ.block_behavior!r} conflicts with "
                        "a block-bypass ability; omitted from block-bypass conclusion"
                    )
                    continue
                assert tokens is not None  # narrowed by has_bypass_ability
                deciding_field = "abilities"
                deciding_value = sorted(tokens & _BYPASS_ABILITIES)[0]
                conf = _CONF_ABILITY_ONLY
                limitations.append(
                    f"{occ.game_id}: block_behavior missing; block bypass inferred from ability"
                )
            else:
                continue

            evidence.append(
                EvidenceItem(
                    ref=occ.game_id,
                    field=deciding_field,
                    value=deciding_value,
                    note=count_note(occ.total_count),
                )
            )
            confidence = max(confidence, conf)

        if not evidence:
            return RuleResult(warnings=tuple(warnings))

        return RuleResult(
            observation=Observation(
                rule_id=RULE_ID,
                category="threat",
                tag="block_bypass",
                title="Block-bypassing enemies present",
                summary=_summary(distinct_refs(evidence)),
                confidence=confidence,
                evidence=tuple(evidence),
                limitations=tuple(limitations),
            ),
            warnings=tuple(warnings),
        )
