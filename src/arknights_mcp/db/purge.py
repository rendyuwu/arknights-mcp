"""Source purge + rebuild (§T26; §V20; PRD §10.8, §11.7).

``purge --rebuild`` removes only the rows attributable to one source and rebuilds
a validated candidate, leaving the current database active until the rebuild
validates and is promoted atomically (§V20). Because releases ship no raw
snapshots (§V16), the rebuild is a *filtered copy* of the active build: copy it,
delete every row that traces to the purged source's snapshots (via
``record_provenance`` -> ``source_snapshots.source_id``), re-materialize the
policy-event journal, validate, and promote. Nothing is ever deleted from the
active build in place (§V4).

Deletion runs children-before-parents with foreign keys enforced, so a shared
entity still referenced by a *non-purged* source raises rather than corrupting
the graph (fail-closed); validation's ``foreign_key_check`` is the backstop.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from arknights_mcp.db.migrations import open_writable
from arknights_mcp.db.policy_events import PolicyEvent, materialize_policy_events
from arknights_mcp.db.promotion import PromotionResult, promote_candidate
from arknights_mcp.db.validate import ValidationReport, validate_database
from arknights_mcp.util.sqlite import integrity_guard


class PurgeError(RuntimeError):
    """Raised when there is no active database to purge from."""


@dataclass(frozen=True)
class PurgeResult:
    """Outcome of :func:`purge_and_rebuild`."""

    affected: dict[str, int]
    validation_passed: bool
    report: ValidationReport
    promotion: PromotionResult | None


def _select_ids(conn: sqlite3.Connection, sql_template: str, ids: Sequence[object]) -> list[object]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    # Only the placeholder marks (?,?,...) are interpolated; every value is bound.
    sql = sql_template % placeholders  # noqa: S608 - placeholders only, values bound
    return [row[0] for row in conn.execute(sql, tuple(ids))]


def _delete_in(conn: sqlite3.Connection, table: str, column: str, ids: Sequence[object]) -> int:
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    # table/column are fixed identifiers from this module; ids are bound (§V2).
    sql = f"DELETE FROM {table} WHERE {column} IN ({placeholders})"  # noqa: S608
    return conn.execute(sql, tuple(ids)).rowcount


def _purge_source_rows(conn: sqlite3.Connection, source_id: str) -> dict[str, int]:
    """Delete every row attributable to ``source_id`` (children first, FKs on).

    Deletes only rows that trace to the source's own snapshots. A shared entity
    still referenced by a *non-purged* source is deleted here only via its own
    (purged-source) parent; if a non-purged row still references it, the parent
    delete raises ``IntegrityError`` and the caller aborts (fail-closed, §V20) --
    it never strips occurrences from a non-purged stage.
    """
    snapshot_ids = [
        row[0]
        for row in conn.execute(
            "SELECT snapshot_id FROM source_snapshots WHERE source_id = ?", (source_id,)
        )
    ]
    prov_ids = _select_ids(
        conn, "SELECT provenance_id FROM record_provenance WHERE snapshot_id IN (%s)", snapshot_ids
    )
    enemy_pks = _select_ids(
        conn, "SELECT enemy_pk FROM enemies WHERE provenance_id IN (%s)", prov_ids
    )
    stage_pks = _select_ids(
        conn, "SELECT stage_pk FROM stages WHERE provenance_id IN (%s)", prov_ids
    )
    wave_pks = _select_ids(
        conn, "SELECT wave_pk FROM stage_waves WHERE stage_pk IN (%s)", stage_pks
    )
    operator_pks = _select_ids(
        conn, "SELECT operator_pk FROM operators WHERE provenance_id IN (%s)", prov_ids
    )
    skill_pks = _select_ids(
        conn, "SELECT skill_pk FROM skills WHERE provenance_id IN (%s)", prov_ids
    )
    module_pks = _select_ids(
        conn, "SELECT module_pk FROM modules WHERE provenance_id IN (%s)", prov_ids
    )
    talent_pks = _select_ids(
        conn, "SELECT talent_pk FROM talents WHERE operator_pk IN (%s)", operator_pks
    )

    # stage domain: children -> parents. stage_spawns / stage_enemies are removed
    # only within the purged source's own waves/stages (by wave_pk / stage_pk),
    # never by enemy_pk -- deleting by enemy_pk would silently strip a purged
    # enemy's occurrences from a *non-purged* stage (L6). If a non-purged stage
    # still references a purged enemy, deleting that enemy below raises (§V20).
    _delete_in(conn, "stage_spawns", "wave_pk", wave_pks)
    _delete_in(conn, "stage_enemies", "stage_pk", stage_pks)
    _delete_in(conn, "stage_tiles", "stage_pk", stage_pks)
    _delete_in(conn, "stage_maps", "stage_pk", stage_pks)
    _delete_in(conn, "stage_routes", "stage_pk", stage_pks)
    _delete_in(conn, "stage_waves", "stage_pk", stage_pks)
    _delete_in(conn, "stages", "stage_pk", stage_pks)

    # operator domain: children -> parents (each core row carries its own
    # provenance; sub-tables link through the parent, §12.3).
    _delete_in(conn, "module_levels", "module_pk", module_pks)
    _delete_in(conn, "modules", "module_pk", module_pks)
    _delete_in(conn, "talent_levels", "talent_pk", talent_pks)
    _delete_in(conn, "talents", "talent_pk", talent_pks)
    _delete_in(conn, "operator_skills", "operator_pk", operator_pks)
    _delete_in(conn, "skill_levels", "skill_pk", skill_pks)
    _delete_in(conn, "skills", "skill_pk", skill_pks)
    _delete_in(conn, "operator_phases", "operator_pk", operator_pks)
    _delete_in(conn, "operator_aliases", "operator_pk", operator_pks)
    _delete_in(conn, "operators", "operator_pk", operator_pks)

    # enemy domain + zones + provenance.
    _delete_in(conn, "enemy_levels", "enemy_pk", enemy_pks)
    _delete_in(conn, "enemy_aliases", "enemy_pk", enemy_pks)
    _delete_in(conn, "enemies", "enemy_pk", enemy_pks)
    _delete_in(conn, "zones", "provenance_id", prov_ids)
    _delete_in(conn, "record_provenance", "provenance_id", prov_ids)
    _delete_in(conn, "source_snapshots", "snapshot_id", snapshot_ids)

    # Keep the data_sources row but reflect the disabled posture in the rebuild.
    conn.execute("UPDATE data_sources SET enabled = 0 WHERE source_id = ?", (source_id,))

    return {
        "snapshots": len(snapshot_ids),
        "enemies": len(enemy_pks),
        "stages": len(stage_pks),
        "operators": len(operator_pks),
    }


def purge_and_rebuild(
    active_db: str | Path,
    source_id: str,
    *,
    data_dir: str | Path,
    servers: Sequence[str] = ("en", "cn"),
    retain_versions: int = 3,
    current_manifest_path: str | Path | None = None,
    policy_events: Sequence[PolicyEvent] = (),
    expected_schema_version: str | None = None,
) -> PurgeResult:
    """Rebuild a candidate with ``source_id``'s rows removed and promote iff valid.

    The active database is only copied (never mutated in place, §V4). If the
    rebuilt candidate fails validation the current build stays active and no
    promotion happens (§V20).
    """
    active = Path(active_db)
    if not active.is_file():
        raise PurgeError("no active database to purge from")

    with tempfile.TemporaryDirectory(prefix="arkmcp-purge-") as tmp:
        candidate = Path(tmp) / "candidate.sqlite"
        shutil.copyfile(active, candidate)

        conn = open_writable(candidate)
        try:
            # A shared entity still referenced by a non-purged source raises
            # IntegrityError: fail closed rather than corrupt the graph. The
            # current build stays active because promotion never runs (§V33/§V20).
            with integrity_guard(
                lambda exc: (
                    f"cannot purge {source_id!r}: a row it owns is still referenced by "
                    f"another source (shared entity); resolve before purging ({exc})"
                ),
                PurgeError,
                on_error=conn.rollback,
            ):
                affected = _purge_source_rows(conn, source_id)
                materialize_policy_events(conn, policy_events)
                conn.commit()
        finally:
            conn.close()

        report = validate_database(
            candidate, expected_schema_version=expected_schema_version, min_snapshots=0
        )
        if not report.passed:
            return PurgeResult(affected, validation_passed=False, report=report, promotion=None)

        promotion = promote_candidate(
            candidate,
            data_dir=data_dir,
            validation_passed=True,
            servers=tuple(servers),
            retain_versions=retain_versions,
            current_manifest_path=current_manifest_path,
        )
    return PurgeResult(affected, validation_passed=True, report=report, promotion=promotion)
