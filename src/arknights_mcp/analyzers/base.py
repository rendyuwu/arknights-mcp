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
    reduces confidence); ``abilities == ()`` means present-but-empty.
    """

    game_id: str
    display_name: str | None
    motion_type: str | None
    attack_type: str | None
    abilities: tuple[str, ...] | None
    total_count: int | None


@dataclass(frozen=True)
class StageThreatContext:
    """Typed input to the stage analyzer for one ``(server, stage)``."""

    server: str
    stage_code: str | None
    occurrences: tuple[EnemyOccurrence, ...]


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
    """A deterministic, typed-field-only stage threat rule (§V26)."""

    rule_id: str

    def evaluate(self, ctx: StageThreatContext) -> RuleResult: ...
