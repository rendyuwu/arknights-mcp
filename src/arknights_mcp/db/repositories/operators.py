"""Operator read repository (§V2; §T20/§T44).

Encapsulates the parameterized ``SELECT``s that back the ``get_operator`` service:
a single operator keyed by ``(server, game_id)`` -- the unique identity -- with its
region-scoped provenance joined in (§V5), plus the operator's opt-in heavy sections
(phases, skills + skill levels, talents + talent variants, modules + module levels)
and cheap section counts for the always-on summary. Rows are returned as flat,
typed dataclasses that mirror the selected columns 1:1; domain shaping (JSON
decode, envelope mapping) stays in the service.

The operator join is on NOT NULL foreign keys
(``operators -> record_provenance -> source_snapshots``), so a found operator always
carries ``snapshot_id`` + ``imported_at`` (§V5). Every value is bound (§V2); no
method accepts caller SQL and nothing is interpolated into a query string.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arknights_mcp.db.repositories.base import Repository


@dataclass(frozen=True)
class OperatorRow:
    """One operator row plus its joined region provenance (§V5)."""

    operator_pk: int
    server: str
    game_id: str
    display_name: str | None
    rarity: int | None
    profession: str | None
    subclass_id: str | None
    position: str | None
    tag_json: str | None
    obtainable: bool
    snapshot_id: str
    imported_at: str


@dataclass(frozen=True)
class OperatorSectionCounts:
    """How many rows each heavy section holds -- the always-on summary (§V22)."""

    phases: int
    skills: int
    talents: int
    modules: int


@dataclass(frozen=True)
class OperatorPhaseRow:
    """One elite phase of an operator (``operator_phases``)."""

    phase: int
    max_level: int | None
    max_hp: int | None
    atk: int | None
    def_: int | None
    res: int | None
    redeploy_time: int | None
    cost: int | None
    block_count: int | None
    attack_interval: float | None
    range_id: str | None


@dataclass(frozen=True)
class OperatorSkillRow:
    """One operator skill slot joined to its skill metadata (``operator_skills``)."""

    skill_pk: int
    game_id: str
    display_name: str | None
    skill_type: str | None
    sp_type: str | None
    duration_type: str | None
    slot_index: int
    unlock_phase: int | None
    unlock_level: int | None


@dataclass(frozen=True)
class SkillLevelRow:
    """One mastery level of a skill (``skill_levels``).

    ``blackboard_json`` stays a JSON string here (allowlisted + sanitized at import,
    §V18/§V31) and is decoded in the service; ``gameplay_description`` is never
    selected (prose excluded, §V16).
    """

    level: int
    sp_cost: int | None
    initial_sp: int | None
    duration: float | None
    range_id: str | None
    blackboard_json: str | None


@dataclass(frozen=True)
class TalentRow:
    """One talent of an operator (``talents``)."""

    talent_pk: int
    talent_index: int
    display_name: str | None


@dataclass(frozen=True)
class TalentLevelRow:
    """One variant of a talent (``talent_levels``); ``blackboard_json`` decoded later."""

    variant_index: int
    unlock_phase: int | None
    unlock_level: int | None
    potential_rank: int | None
    blackboard_json: str | None


@dataclass(frozen=True)
class ModuleRow:
    """One module of an operator (``modules``)."""

    module_pk: int
    game_id: str
    module_type: str | None
    display_name: str | None
    unlock_phase: int | None
    unlock_level: int | None


@dataclass(frozen=True)
class ModuleLevelRow:
    """One level of a module (``module_levels``); the ``*_json`` bundles decoded later."""

    level: int
    stat_bonus_json: str | None
    trait_changes_json: str | None
    talent_changes_json: str | None
    cost_json: str | None


_OPERATOR_SQL = (
    "SELECT o.operator_pk, o.server, o.game_id, o.display_name, o.rarity, o.profession, "
    "o.subclass_id, o.position, o.tag_json, o.obtainable, p.snapshot_id, ss.imported_at "
    "FROM operators o "
    "JOIN record_provenance p ON p.provenance_id = o.provenance_id "
    "JOIN source_snapshots ss ON ss.snapshot_id = p.snapshot_id "
    "WHERE o.server = ? AND o.game_id = ? "
    "LIMIT 1"
)

# One round-trip for the four summary counts; every value bound (§V2).
_COUNTS_SQL = (
    "SELECT "
    "(SELECT COUNT(*) FROM operator_phases WHERE operator_pk = ?), "
    "(SELECT COUNT(*) FROM operator_skills WHERE operator_pk = ?), "
    "(SELECT COUNT(*) FROM talents WHERE operator_pk = ?), "
    "(SELECT COUNT(*) FROM modules WHERE operator_pk = ?)"
)

_PHASES_SQL = (
    "SELECT phase, max_level, max_hp, atk, def, res, redeploy_time, cost, block_count, "
    "attack_interval, range_id "
    "FROM operator_phases WHERE operator_pk = ? ORDER BY phase"
)

# Skills ordered by the 1-based slot so the emitted list is deterministic.
_SKILLS_SQL = (
    "SELECT s.skill_pk, s.game_id, s.display_name, s.skill_type, s.sp_type, s.duration_type, "
    "os.slot_index, os.unlock_phase, os.unlock_level "
    "FROM operator_skills os JOIN skills s ON s.skill_pk = os.skill_pk "
    "WHERE os.operator_pk = ? ORDER BY os.slot_index"
)

_SKILL_LEVELS_SQL = (
    "SELECT level, sp_cost, initial_sp, duration, range_id, blackboard_json "
    "FROM skill_levels WHERE skill_pk = ? ORDER BY level"
)

_TALENTS_SQL = (
    "SELECT talent_pk, talent_index, display_name "
    "FROM talents WHERE operator_pk = ? ORDER BY talent_index"
)

_TALENT_LEVELS_SQL = (
    "SELECT variant_index, unlock_phase, unlock_level, potential_rank, blackboard_json "
    "FROM talent_levels WHERE talent_pk = ? ORDER BY variant_index"
)

# Modules ordered by game_id (stable identity) so the emitted list is deterministic.
_MODULES_SQL = (
    "SELECT module_pk, game_id, module_type, display_name, unlock_phase, unlock_level "
    "FROM modules WHERE operator_pk = ? ORDER BY game_id"
)

_MODULE_LEVELS_SQL = (
    "SELECT level, stat_bonus_json, trait_changes_json, talent_changes_json, cost_json "
    "FROM module_levels WHERE module_pk = ? ORDER BY level"
)


def _to_operator_row(row: Any) -> OperatorRow:
    (
        operator_pk,
        server,
        game_id,
        display_name,
        rarity,
        profession,
        subclass_id,
        position,
        tag_json,
        obtainable,
        snapshot_id,
        imported_at,
    ) = row
    return OperatorRow(
        operator_pk=operator_pk,
        server=server,
        game_id=game_id,
        display_name=display_name,
        rarity=rarity,
        profession=profession,
        subclass_id=subclass_id,
        position=position,
        tag_json=tag_json,
        obtainable=bool(obtainable),
        snapshot_id=snapshot_id,
        imported_at=imported_at,
    )


class OperatorRepository(Repository):
    """Read-only access to an operator and its heavy sections (§V2)."""

    def operator_by_game_id(self, server: str, game_id: str) -> OperatorRow | None:
        """Operator for ``(server, game_id)`` -- the unique key -- or ``None``."""
        row = self._one(_OPERATOR_SQL, (server, game_id))
        return _to_operator_row(row) if row is not None else None

    def section_counts(self, operator_pk: int) -> OperatorSectionCounts:
        """The four heavy-section row counts for the always-on summary (§V22)."""
        phases, skills, talents, modules = self._one(
            _COUNTS_SQL, (operator_pk, operator_pk, operator_pk, operator_pk)
        )
        return OperatorSectionCounts(phases=phases, skills=skills, talents=talents, modules=modules)

    def phases(self, operator_pk: int) -> list[OperatorPhaseRow]:
        """Every elite phase of the operator, ordered by ``phase``."""
        return [
            OperatorPhaseRow(
                phase=r[0],
                max_level=r[1],
                max_hp=r[2],
                atk=r[3],
                def_=r[4],
                res=r[5],
                redeploy_time=r[6],
                cost=r[7],
                block_count=r[8],
                attack_interval=r[9],
                range_id=r[10],
            )
            for r in self._all(_PHASES_SQL, (operator_pk,))
        ]

    def skills(self, operator_pk: int) -> list[OperatorSkillRow]:
        """Every skill slot of the operator, ordered by ``slot_index``."""
        return [
            OperatorSkillRow(
                skill_pk=r[0],
                game_id=r[1],
                display_name=r[2],
                skill_type=r[3],
                sp_type=r[4],
                duration_type=r[5],
                slot_index=r[6],
                unlock_phase=r[7],
                unlock_level=r[8],
            )
            for r in self._all(_SKILLS_SQL, (operator_pk,))
        ]

    def skill_levels(self, skill_pk: int) -> list[SkillLevelRow]:
        """Every mastery level of a skill, ordered by ``level``."""
        return [
            SkillLevelRow(
                level=r[0],
                sp_cost=r[1],
                initial_sp=r[2],
                duration=r[3],
                range_id=r[4],
                blackboard_json=r[5],
            )
            for r in self._all(_SKILL_LEVELS_SQL, (skill_pk,))
        ]

    def talents(self, operator_pk: int) -> list[TalentRow]:
        """Every talent of the operator, ordered by ``talent_index``."""
        return [
            TalentRow(talent_pk=r[0], talent_index=r[1], display_name=r[2])
            for r in self._all(_TALENTS_SQL, (operator_pk,))
        ]

    def talent_levels(self, talent_pk: int) -> list[TalentLevelRow]:
        """Every variant of a talent, ordered by ``variant_index``."""
        return [
            TalentLevelRow(
                variant_index=r[0],
                unlock_phase=r[1],
                unlock_level=r[2],
                potential_rank=r[3],
                blackboard_json=r[4],
            )
            for r in self._all(_TALENT_LEVELS_SQL, (talent_pk,))
        ]

    def modules(self, operator_pk: int) -> list[ModuleRow]:
        """Every module of the operator, ordered by ``game_id``."""
        return [
            ModuleRow(
                module_pk=r[0],
                game_id=r[1],
                module_type=r[2],
                display_name=r[3],
                unlock_phase=r[4],
                unlock_level=r[5],
            )
            for r in self._all(_MODULES_SQL, (operator_pk,))
        ]

    def module_levels(self, module_pk: int) -> list[ModuleLevelRow]:
        """Every level of a module, ordered by ``level``."""
        return [
            ModuleLevelRow(
                level=r[0],
                stat_bonus_json=r[1],
                trait_changes_json=r[2],
                talent_changes_json=r[3],
                cost_json=r[4],
            )
            for r in self._all(_MODULE_LEVELS_SQL, (module_pk,))
        ]
