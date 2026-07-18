"""Shared vocabulary for the deterministic stage/threat analyzers (§V6, §V26).

Defines the typed inputs a rule reads (:class:`EnemyOccurrence`,
:class:`StageThreatContext`), the evidence-backed :class:`Observation` a rule
emits, and the :class:`ThreatRule` protocol. Every observation carries the five
fields §V6 mandates: ``rule_id`` + ``evidence`` + ``confidence`` +
``limitations`` + ``analyzer_version``. Rules decide from typed fields only --
never from natural-language prose (§V26).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

#: Analyzer-logic version stamped on every observation (§V6). Bump when rule
#: logic changes so a stored observation is attributable to the code that made
#: it (mirrors ``FIELD_POLICY_VERSION`` / ``TRANSFORM_VERSION``).
ANALYZER_VERSION = "1"


@dataclass(frozen=True)
class EnemyOccurrence:
    """One enemy's typed, allowlisted appearance in a stage (rule input).

    ``abilities is None`` means the source field was absent (missing -> §V26
    reduces confidence); ``abilities == ()`` means present-but-empty. The M3 stat
    and timing fields (§T39) follow the same convention: ``None`` = the source
    field was absent, so a rule reduces confidence or records a limitation (§V26),
    never silently treats it as zero.
    """

    game_id: str
    display_name: str | None
    motion_type: str | None
    attack_type: str | None
    abilities: tuple[str, ...] | None
    total_count: int | None
    # M3 rule inputs (§T39): typed stat / timing fields from the enemy's level
    # variant and its stage occurrence. Defaulted so the M0 aerial substrate (which
    # reads only motion + abilities) constructs unchanged.
    defense: int | None = None
    res: int | None = None
    attack_range: float | None = None
    block_behavior: str | None = None
    first_spawn_time: float | None = None
    last_spawn_time: float | None = None
    route_count: int | None = None


@dataclass(frozen=True)
class StageTiles:
    """Deploy-surface summary of a stage's tile grid (tiles/deploy rule input; §T39).

    Counts are derived from typed tile fields (``buildable_type`` + ``height_type``):
    a ground (LOWLAND) buildable tile holds a melee unit, a high-ground (HIGHLAND)
    buildable tile holds a ranged unit. When a stage carries no tile rows the
    context passes ``tiles=None`` rather than an all-zero summary, so a rule skips
    absent data instead of judging it (§V26).
    """

    total: int
    buildable_melee: int
    buildable_ranged: int


@dataclass(frozen=True)
class StageThreatContext:
    """Typed input to the stage analyzer for one ``(server, stage)``."""

    server: str
    stage_code: str | None
    occurrences: tuple[EnemyOccurrence, ...]
    # M3 stage-level rule inputs (§T39): the count of distinct enemy routes and the
    # deploy-tile summary. ``None`` = the datum was not loaded, so the lane/route or
    # tiles/deploy rule skips it (§V26) rather than concluding from absent data.
    route_count: int | None = None
    tiles: StageTiles | None = None


@dataclass(frozen=True)
class EvidenceItem:
    """A single typed datum that drove an observation (§V6 evidence)."""

    ref: str  # what the datum is about (an enemy ``game_id``)
    field: str  # the typed source field it came from
    value: Any  # the observed typed value
    note: str | None = None


@dataclass(frozen=True)
class Observation:
    """An evidence-backed analyzer conclusion (§V6). Never a recommendation."""

    rule_id: str
    category: str
    tag: str
    title: str
    summary: str
    confidence: float
    evidence: tuple[EvidenceItem, ...]
    limitations: tuple[str, ...]
    analyzer_version: str = ANALYZER_VERSION


@dataclass(frozen=True)
class RuleResult:
    """A rule's output: an optional observation plus any §V26 warnings.

    A warning without an observation records a conflict/omission the rule could
    not turn into a conclusion (§V26 "conflicting source fields -> omit + warn").
    """

    observation: Observation | None = None
    warnings: tuple[str, ...] = ()


@runtime_checkable
class ThreatRule(Protocol):
    """A deterministic, typed-field-only stage threat rule (§V26).

    ``rule_id`` is a read-only property so a rule may expose it as a plain class
    attribute *or* as a frozen-dataclass field (the shared ``AbilityTokenRule`` is
    frozen); a rule never mutates its own id.
    """

    @property
    def rule_id(self) -> str: ...

    def evaluate(self, ctx: StageThreatContext) -> RuleResult: ...
