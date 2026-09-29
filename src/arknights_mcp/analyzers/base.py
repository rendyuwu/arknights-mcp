"""Shared vocabulary for the deterministic stage/threat analyzers.

Defines the typed inputs a rule reads (:class:`EnemyOccurrence`,
:class:`StageThreatContext`), the evidence-backed :class:`Observation` a rule
emits, and the :class:`ThreatRule` protocol. Every observation carries the five
mandated fields: ``rule_id`` + ``evidence`` + ``confidence`` +
``limitations`` + ``analyzer_version``. Rules decide from typed fields only --
never from natural-language prose.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

#: Analyzer-logic version stamped on every observation. Bump when rule
#: logic changes so a stored observation is attributable to the code that made
#: it (mirrors ``FIELD_POLICY_VERSION`` / ``TRANSFORM_VERSION``).
ANALYZER_VERSION = "1"


@dataclass(frozen=True)
class EnemyOccurrence:
    """One enemy's typed, allowlisted appearance in a stage (rule input).

    ``damage_types is None`` means the source field was absent (a missing field
    reduces confidence); ``damage_types == ()`` means present-but-empty. The M3 stat
    and timing fields follow the same convention: ``None`` = the source
    field was absent, so a rule reduces confidence or records a limitation,
    never silently treats it as zero.

    Every field here is one a real build POPULATES. ``abilities`` / ``block_behavior``
    were removed: no upstream source carries either, so the three rules
    that decided from them (block-bypass, crowd-control, support-aura) were retired --
    a rule input nothing can fill is not a conservative default, it is a
    guaranteed "missing field" arm that makes a dead rule read as a clean one.
    """

    game_id: str
    display_name: str | None
    motion_type: str | None
    #: The enemy's damage kinds (upstream ``damageType``): a LIST, because 42 real
    #: enemies deal PHYSIC *and* MAGIC. Replaces the retired ``attack_type`` scalar,
    #: which is NULL on 3879/3879 build rows.
    damage_types: tuple[str, ...] | None
    total_count: int | None
    # M3 rule inputs: typed stat / timing fields from the enemy's level
    # variant and its stage occurrence. Defaulted so the M0 aerial substrate (which
    # reads only motion) constructs unchanged.
    defense: int | None = None
    res: int | None = None
    attack_range: float | None = None
    #: ``True`` when the source DECLARED this enemy has no attack radius
    #: (its ``-1.0`` sentinel, kept out of the distance column), ``False`` when
    #: it declared a radius or said nothing. Read BEFORE ``targeting``, because an absent
    #: ``attack_range`` alone cannot tell a denied radius from an unstated one, and a rule
    #: must not report a cell the source FILLED as missing.
    attack_range_declared_none: bool = False
    #: Upstream ``applyWay``: MELEE / RANGED / ALL / NONE, the enemy's own statement
    #: of what it can reach. Read BEFORE any inference about reach.
    targeting: str | None = None
    first_spawn_time: float | None = None
    last_spawn_time: float | None = None
    route_count: int | None = None


@dataclass(frozen=True)
class StageTiles:
    """Deploy-surface summary of a stage's tile grid (tiles/deploy rule input).

    Counts are derived from typed tile fields (``buildable_type`` + ``height_type``):
    a ground (LOWLAND) buildable tile holds a melee unit, a high-ground (HIGHLAND)
    buildable tile holds a ranged unit. When a stage carries no tile rows the
    context passes ``tiles=None`` rather than an all-zero summary, so a rule skips
    absent data instead of judging it.
    """

    total: int
    buildable_melee: int
    buildable_ranged: int


@dataclass(frozen=True)
class StageThreatContext:
    """Typed input to the stage analyzer for one ``(server, stage)``.

    ``stage_game_id`` is REQUIRED and is what a stage-level evidence row refs:
    ``stage_code`` is shared by the normal/tough/challenge variants of one stage, so an
    observation reffing ``"14-18"`` is undecidable and un-joinable to the stage block,
    which is keyed on ``game_id``. It is deliberately not defaulted -- an
    ``or stage_code`` fallback would re-admit that bug silently the first time a caller
    forgot it. ``stage_code`` stays for the human-readable summary text only.
    """

    server: str
    stage_game_id: str
    stage_code: str | None
    occurrences: tuple[EnemyOccurrence, ...]
    # M3 stage-level rule inputs: the count of distinct enemy routes and the
    # deploy-tile summary. ``None`` = the datum was not loaded, so the lane/route or
    # tiles/deploy rule skips it rather than concluding from absent data.
    route_count: int | None = None
    tiles: StageTiles | None = None


@dataclass(frozen=True)
class EvidenceItem:
    """A single typed datum that drove an observation.

    Evidence must EXIST; the row shape below says what one row IS. The four rules,
    all of them testable:

    * ``ref`` -- an unambiguous stable id: an enemy/module ``game_id``, or a
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
      belong in a note is the level list a deduped row carries, because ``count``
      is dedup multiplicity and the tuple has no level slot.
    """

    ref: str  # what the datum is about (an enemy / module / stage ``game_id``)
    field: str  # the emitted field path, relative to that record
    value: Any  # that field's typed scalar
    note: str | None = None
    #: How many byte-identical source rows this row stands for after dedup
    #: (``None`` = the row was unique; the wire mapping omits the key then).
    count: int | None = None


def dedupe_evidence(evidence: Iterable[EvidenceItem]) -> tuple[EvidenceItem, ...]:
    """Collapse byte-identical evidence rows into one attributed row.

    Identity is ``ref`` + ``field`` + ``value`` (keyed on ``repr`` so ``14`` and
    ``14.0`` stay distinct). The collapsed row keeps first-seen order, joins its
    distinct notes with ``"; "`` in first-seen order (per-level notes read as a
    level list) and carries ``count`` = the number of source rows it stands for
    -- N verbatim repeats never reach the client while the attribution
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
    """An evidence-backed analyzer conclusion. Never a recommendation."""

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
    """A rule's output: an optional observation plus any warnings.

    A warning without an observation records a conflict/omission the rule could
    not turn into a conclusion.
    """

    observation: Observation | None = None
    warnings: tuple[str, ...] = ()


@runtime_checkable
class ThreatRule(Protocol):
    """A deterministic, typed-field-only stage threat rule.

    ``rule_id`` is a read-only property so a rule may expose it as a plain class
    attribute *or* as a frozen-dataclass field (the shared ``AbilityTokenRule`` is
    frozen); a rule never mutates its own id.
    """

    @property
    def rule_id(self) -> str: ...

    def evaluate(self, ctx: StageThreatContext) -> RuleResult: ...
