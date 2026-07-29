"""Shared vocabulary for the deterministic stage/threat analyzers (§V6, §V26).

Defines the typed inputs a rule reads (:class:`EnemyOccurrence`,
:class:`StageThreatContext`), the evidence-backed :class:`Observation` a rule
emits, and the :class:`ThreatRule` protocol. Every observation carries the five
fields §V6 mandates: ``rule_id`` + ``evidence`` + ``confidence`` +
``limitations`` + ``analyzer_version``. Rules decide from typed fields only --
never from natural-language prose (§V26).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

#: Analyzer-logic version stamped on every observation (§V6). Bump when rule
#: logic changes so a stored observation is attributable to the code that made
#: it (mirrors ``FIELD_POLICY_VERSION`` / ``TRANSFORM_VERSION``).
ANALYZER_VERSION = "1"


@dataclass(frozen=True)
class EnemyOccurrence:
    """One enemy's typed, allowlisted appearance in a stage (rule input).

    ``damage_types is None`` means the source field was absent (missing -> §V26
    reduces confidence); ``damage_types == ()`` means present-but-empty. The M3 stat
    and timing fields (§T39) follow the same convention: ``None`` = the source
    field was absent, so a rule reduces confidence or records a limitation (§V26),
    never silently treats it as zero.

    Every field here is one a real build POPULATES. ``abilities`` / ``block_behavior``
    were removed in §T210 (c): no upstream source carries either, so the three rules
    that decided from them (block-bypass, crowd-control, support-aura) were retired
    (B160 (c)) -- a rule input nothing can fill is not a conservative default, it is a
    guaranteed §V26 "missing field" arm that makes a dead rule read as a clean one.
    """

    game_id: str
    display_name: str | None
    motion_type: str | None
    #: The enemy's damage kinds (upstream ``damageType``): a LIST, because 42 real
    #: enemies deal PHYSIC *and* MAGIC. Replaces the retired ``attack_type`` scalar,
    #: which is NULL on 3879/3879 build rows (§V113 retired arm; B160 (b)).
    damage_types: tuple[str, ...] | None
    total_count: int | None
    # M3 rule inputs (§T39): typed stat / timing fields from the enemy's level
    # variant and its stage occurrence. Defaulted so the M0 aerial substrate (which
    # reads only motion) constructs unchanged.
    defense: int | None = None
    res: int | None = None
    attack_range: float | None = None
    #: Upstream ``applyWay``: MELEE / RANGED / ALL / NONE, the enemy's own statement
    #: of what it can reach. Read BEFORE any inference about reach (§T210 (b)).
    targeting: str | None = None
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
    """Typed input to the stage analyzer for one ``(server, stage)``.

    ``stage_game_id`` is REQUIRED and is what a stage-level evidence row refs (§V68):
    ``stage_code`` is shared by the normal/tough/challenge variants of one stage, so an
    observation reffing ``"14-18"`` is undecidable and un-joinable to the stage block,
    which is keyed on ``game_id`` (B57/B136). It is deliberately not defaulted -- an
    ``or stage_code`` fallback would re-admit that bug silently the first time a caller
    forgot it. ``stage_code`` stays for the human-readable summary text only.
    """

    server: str
    stage_game_id: str
    stage_code: str | None
    occurrences: tuple[EnemyOccurrence, ...]
    # M3 stage-level rule inputs (§T39): the count of distinct enemy routes and the
    # deploy-tile summary. ``None`` = the datum was not loaded, so the lane/route or
    # tiles/deploy rule skips it (§V26) rather than concluding from absent data.
    route_count: int | None = None
    tiles: StageTiles | None = None


@dataclass(frozen=True)
class EvidenceItem:
    """A single typed datum that drove an observation (§V6 evidence, §V101 shape).

    §V6 says evidence must EXIST; §V101 says what one row IS (B137). The four rules,
    all of them testable:

    * ``ref`` -- an unambiguous stable id (§V68): an enemy/module ``game_id``, or a
      stage's ``game_id`` -- never a ``stage_code``, which several stage variants share.
    * ``field`` -- a REAL emitted field path, relative to the record ``ref`` names
      (``def`` and ``total_count`` on an enemy occurrence, ``metrics.tile_total`` on the
      stage block, ``stat_bonus.atk`` on a module). Never a packed pseudo-path like
      ``"def/res"``, which resolves to nothing a client can look up.
    * ``value`` -- THAT field's scalar. Never a packed string (``"def=200,res=50"``),
      never a magnitude-free ``1`` that only restates presence.
    * ``note`` -- PROSE ONLY. A number a client may USE never lives here: it is a fact
      with a field path of its own, so it gets its own row (one fact per row, a packed
      pair becomes two rows plus the comparison in the note). The one number that does
      belong in a note is the §V85 level list a deduped row carries, because ``count``
      is dedup multiplicity and the tuple has no level slot.
    """

    ref: str  # what the datum is about (an enemy / module / stage ``game_id``)
    field: str  # the emitted field path, relative to that record
    value: Any  # that field's typed scalar
    note: str | None = None
    #: How many byte-identical source rows this row stands for after §V85 dedup
    #: (``None`` = the row was unique; the wire mapping omits the key then, §V67).
    count: int | None = None


def dedupe_evidence(evidence: Iterable[EvidenceItem]) -> tuple[EvidenceItem, ...]:
    """Collapse byte-identical evidence rows into one attributed row (§V85, B92).

    Identity is ``ref`` + ``field`` + ``value`` (keyed on ``repr`` so ``14`` and
    ``14.0`` stay distinct). The collapsed row keeps first-seen order, joins its
    distinct notes with ``"; "`` in first-seen order (per-level notes read as a
    level list) and carries ``count`` = the number of source rows it stands for
    -- N verbatim repeats never reach the client while the §V6 attribution
    survives in the one row kept. A unique row passes through unchanged.
    """
    groups: dict[tuple[str, str, str], list[EvidenceItem]] = {}
    for item in evidence:
        groups.setdefault((item.ref, item.field, repr(item.value)), []).append(item)
    out: list[EvidenceItem] = []
    for items in groups.values():
        first = items[0]
        if len(items) == 1:
            out.append(first)
            continue
        notes = list(dict.fromkeys(i.note for i in items if i.note is not None))
        out.append(
            EvidenceItem(
                ref=first.ref,
                field=first.field,
                value=first.value,
                note="; ".join(notes) if notes else None,
                count=len(items),
            )
        )
    return tuple(out)


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
