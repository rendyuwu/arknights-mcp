"""Internal stage analysis service (§T17): the single domain entry point both
transports call to analyze a stage (§V14).

Given a read-only SQLite connection and a ``(server, stage)`` selector, it loads
the stage facts + region + provenance (§V5) and the stage's typed enemy
occurrences, builds a :class:`~arknights_mcp.analyzers.base.StageThreatContext`,
and runs the deterministic threat analyzer. Every observation it returns keeps
the five §V6 fields the analyzer stamped (``rule_id`` + evidence + confidence +
limitations + ``analyzer_version``); the service adds no natural-language
interpretation of its own.

Read-only + parameterized SQL only (§V2): the service issues ``SELECT`` with
``?`` placeholders and never mutates the database. It does not open the
connection (the read-only connection factory is §T20); callers pass one in, so
both transports share this exact function (§V14). No transport-specific logic
lives here.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal

from arknights_mcp.analyzers import (
    EnemyOccurrence,
    Observation,
    StageThreatContext,
)
from arknights_mcp.analyzers import (
    analyze_stage as run_threat_analysis,
)

#: Typed outcome of a stage lookup. The full §V23 status vocabulary is wired
#: into the tool envelope in §T29; the M0 service reports only these two.
StageAnalysisStatus = Literal["ok", "not_found"]


@dataclass(frozen=True)
class StageProvenance:
    """Region-scoped provenance for a factual stage response (§V5)."""

    snapshot_id: str
    imported_at: str


@dataclass(frozen=True)
class StageFacts:
    """Typed, allowlisted facts about one stage (no prose; §V16, §V18)."""

    server: str
    game_id: str
    stage_code: str | None
    display_name: str | None
    zone_game_id: str | None
    stage_type: str | None
    difficulty: str | None
    sanity_cost: int | None
    recommended_level: int | None
    max_life_points: int | None
    provenance: StageProvenance


@dataclass(frozen=True)
class EnemyOccurrenceFacts:
    """One enemy's typed appearance in the stage (from ``stage_enemies``)."""

    game_id: str
    display_name: str | None
    enemy_class: str | None
    is_boss: bool
    is_elite: bool
    motion_type: str | None
    attack_type: str | None
    level_variant: int
    total_count: int | None
    first_spawn_time: float | None
    last_spawn_time: float | None
    route_count: int | None


@dataclass(frozen=True)
class StageAnalysisResult:
    """Domain result of the stage analysis service.

    Carries region (``server``) + provenance on the facts (§V5) and the
    evidence-backed observations (§V6). ``status == "not_found"`` implies
    ``stage is None`` and empty occurrences/observations.
    """

    status: StageAnalysisStatus
    server: str
    stage: StageFacts | None
    occurrences: tuple[EnemyOccurrenceFacts, ...]
    observations: tuple[Observation, ...]
    warnings: tuple[str, ...]
    analyzer_version: str | None


# stages -> record_provenance -> source_snapshots gives snapshot_id + imported_at
# for every stage (both joins are on NOT NULL FKs), so §V5 provenance is always
# present on an "ok" result. Column names are literal; every value is bound (§V2).
_STAGE_SELECT = (
    "SELECT s.stage_pk, s.server, s.game_id, s.stage_code, s.display_name, "
    "z.game_id, s.stage_type, s.difficulty, s.sanity_cost, "
    "s.recommended_level, s.max_life_points, p.snapshot_id, ss.imported_at "
    "FROM stages s "
    "JOIN record_provenance p ON p.provenance_id = s.provenance_id "
    "JOIN source_snapshots ss ON ss.snapshot_id = p.snapshot_id "
    "LEFT JOIN zones z ON z.zone_pk = s.zone_pk "
    "WHERE s.server = ? AND "
)
_STAGE_BY_CODE_SQL = _STAGE_SELECT + "s.stage_code = ? ORDER BY s.stage_pk LIMIT 1"
_STAGE_BY_GAME_ID_SQL = _STAGE_SELECT + "s.game_id = ? ORDER BY s.stage_pk LIMIT 1"

_OCCURRENCES_SQL = (
    "SELECT e.game_id, e.display_name, e.enemy_class, e.is_boss, e.is_elite, "
    "e.motion_type, e.attack_type, se.enemy_level_variant, se.total_count, "
    "se.first_spawn_time, se.last_spawn_time, se.route_count, el.abilities_json "
    "FROM stage_enemies se "
    "JOIN enemies e ON e.enemy_pk = se.enemy_pk "
    "LEFT JOIN enemy_levels el "
    "ON el.enemy_pk = se.enemy_pk AND el.level_variant = se.enemy_level_variant "
    "WHERE se.stage_pk = ? "
    "ORDER BY e.game_id, se.enemy_level_variant"
)


def _parse_abilities(raw: str | None) -> tuple[str, ...] | None:
    """Decode ``enemy_levels.abilities_json`` preserving the §V26 missing/empty
    distinction the analyzer relies on: SQL ``NULL`` -> ``None`` (field absent),
    ``"[]"`` -> ``()`` (present but empty)."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return tuple(str(a) for a in data)


def _not_found(server: str) -> StageAnalysisResult:
    return StageAnalysisResult(
        status="not_found",
        server=server,
        stage=None,
        occurrences=(),
        observations=(),
        warnings=(),
        analyzer_version=None,
    )


def analyze_stage(
    conn: sqlite3.Connection,
    *,
    server: str,
    stage_code: str | None = None,
    game_id: str | None = None,
) -> StageAnalysisResult:
    """Analyze one stage for ``server``, selected by ``game_id`` (preferred, the
    unique key) or ``stage_code``. Read-only; parameterized SQL only (§V2).

    Returns a :class:`StageAnalysisResult` with region + provenance on the facts
    (§V5) and the analyzer's evidence-backed observations (§V6). Both transports
    call this same function (§V14).
    """
    if game_id is not None:
        row = conn.execute(_STAGE_BY_GAME_ID_SQL, (server, game_id)).fetchone()
    elif stage_code is not None:
        row = conn.execute(_STAGE_BY_CODE_SQL, (server, stage_code)).fetchone()
    else:
        raise ValueError("analyze_stage requires stage_code or game_id")

    if row is None:
        return _not_found(server)

    (
        stage_pk,
        stage_server,
        stage_game_id,
        code,
        display_name,
        zone_game_id,
        stage_type,
        difficulty,
        sanity_cost,
        recommended_level,
        max_life_points,
        snapshot_id,
        imported_at,
    ) = row

    facts = StageFacts(
        server=stage_server,
        game_id=stage_game_id,
        stage_code=code,
        display_name=display_name,
        zone_game_id=zone_game_id,
        stage_type=stage_type,
        difficulty=difficulty,
        sanity_cost=sanity_cost,
        recommended_level=recommended_level,
        max_life_points=max_life_points,
        provenance=StageProvenance(snapshot_id=snapshot_id, imported_at=imported_at),
    )

    occurrences: list[EnemyOccurrenceFacts] = []
    threat_inputs: list[EnemyOccurrence] = []
    for (
        occ_game_id,
        occ_name,
        enemy_class,
        is_boss,
        is_elite,
        motion_type,
        attack_type,
        level_variant,
        total_count,
        first_spawn_time,
        last_spawn_time,
        route_count,
        abilities_json,
    ) in conn.execute(_OCCURRENCES_SQL, (stage_pk,)):
        occurrences.append(
            EnemyOccurrenceFacts(
                game_id=occ_game_id,
                display_name=occ_name,
                enemy_class=enemy_class,
                is_boss=bool(is_boss),
                is_elite=bool(is_elite),
                motion_type=motion_type,
                attack_type=attack_type,
                level_variant=level_variant,
                total_count=total_count,
                first_spawn_time=first_spawn_time,
                last_spawn_time=last_spawn_time,
                route_count=route_count,
            )
        )
        threat_inputs.append(
            EnemyOccurrence(
                game_id=occ_game_id,
                display_name=occ_name,
                motion_type=motion_type,
                attack_type=attack_type,
                abilities=_parse_abilities(abilities_json),
                total_count=total_count,
            )
        )

    analysis = run_threat_analysis(
        StageThreatContext(
            server=stage_server,
            stage_code=code,
            occurrences=tuple(threat_inputs),
        )
    )

    return StageAnalysisResult(
        status="ok",
        server=stage_server,
        stage=facts,
        occurrences=tuple(occurrences),
        observations=analysis.observations,
        warnings=analysis.warnings,
        analyzer_version=analysis.analyzer_version,
    )
