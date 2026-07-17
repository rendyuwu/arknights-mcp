"""Candidate validation gate (§T23; §V4).

A candidate is promotable only after it passes this gate (§V4): SQLite
``PRAGMA integrity_check`` + ``PRAGMA foreign_key_check``, a critical-table
presence check, row-count sanity, an orphan/cross-region consistency check, an
FTS smoke test (skipped until the FTS index exists, §T31), and golden domain
invariants (regions confined to ``{en, cn}``; the schema version matches the
applied migrations). The report is data only -- the caller decides whether to
promote -- so ``sync``/``import``/``purge`` all gate promotion on the same result
and the ``validate`` CLI command can print it without side effects.

Read-only: the candidate is opened through the read-only connection factory
(§V2); validation never mutates it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from arknights_mcp.db.connection import DatabaseUnavailable, open_read_only

#: Tables that must exist for a build to be usable (the M1 domains + metadata).
CRITICAL_TABLES: tuple[str, ...] = (
    "schema_migrations",
    "data_sources",
    "source_snapshots",
    "record_provenance",
    "source_policy_events",
    "enemies",
    "enemy_levels",
    "zones",
    "stages",
    "stage_maps",
    "stage_tiles",
    "stage_routes",
    "stage_waves",
    "stage_spawns",
    "stage_enemies",
    "operators",
    "skills",
    "modules",
    "analysis_rules",
    "analysis_findings",
)

#: Regions confined to the v0.1 set (§C, §V5).
VALID_REGIONS = frozenset({"en", "cn"})


@dataclass(frozen=True)
class CheckResult:
    """One validation check outcome."""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class ValidationReport:
    """Aggregate validation outcome; ``passed`` is true iff every check passed."""

    passed: bool
    schema_version: str | None
    checks: tuple[CheckResult, ...] = field(default_factory=tuple)

    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "schema_version": self.schema_version,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
        }


def _integrity_check(conn: sqlite3.Connection) -> CheckResult:
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    ok = len(rows) == 1 and rows[0][0] == "ok"
    detail = "ok" if ok else "; ".join(str(r[0]) for r in rows[:5])
    return CheckResult("integrity_check", ok, detail)


def _foreign_key_check(conn: sqlite3.Connection) -> CheckResult:
    rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    if not rows:
        return CheckResult("foreign_key_check", True, "no violations")
    sample = "; ".join(f"{r[0]}#{r[1]}->{r[2]}" for r in rows[:5])
    return CheckResult("foreign_key_check", False, f"{len(rows)} violation(s): {sample}")


def _critical_tables(conn: sqlite3.Connection) -> CheckResult:
    present = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = [t for t in CRITICAL_TABLES if t not in present]
    if missing:
        return CheckResult("critical_tables", False, f"missing: {', '.join(missing)}")
    return CheckResult("critical_tables", True, f"{len(CRITICAL_TABLES)} present")


def _row_counts(conn: sqlite3.Connection, *, min_snapshots: int) -> CheckResult:
    migrations = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    sources = conn.execute("SELECT COUNT(*) FROM data_sources").fetchone()[0]
    snapshots = conn.execute("SELECT COUNT(*) FROM source_snapshots").fetchone()[0]
    problems: list[str] = []
    if migrations < 1:
        problems.append("no applied migrations")
    if sources < 1:
        problems.append("no data_sources rows")
    if snapshots < min_snapshots:
        problems.append(f"snapshots {snapshots} < required {min_snapshots}")
    if problems:
        return CheckResult("row_counts", False, "; ".join(problems))
    return CheckResult(
        "row_counts", True, f"migrations={migrations} sources={sources} snapshots={snapshots}"
    )


def _orphans(conn: sqlite3.Connection) -> CheckResult:
    """Cross-region / logical orphans not caught by declared foreign keys (§V5)."""
    mismatched = conn.execute(
        "SELECT COUNT(*) FROM stage_enemies se "
        "JOIN stages s ON s.stage_pk = se.stage_pk "
        "JOIN enemies e ON e.enemy_pk = se.enemy_pk "
        "WHERE s.server <> e.server"
    ).fetchone()[0]
    spawn_mismatch = conn.execute(
        "SELECT COUNT(*) FROM stage_spawns sp "
        "JOIN stage_waves w ON w.wave_pk = sp.wave_pk "
        "JOIN stages s ON s.stage_pk = w.stage_pk "
        "JOIN enemies e ON e.enemy_pk = sp.enemy_pk "
        "WHERE s.server <> e.server"
    ).fetchone()[0]
    total = mismatched + spawn_mismatch
    if total:
        return CheckResult("orphans", False, f"{total} cross-region enemy reference(s)")
    return CheckResult("orphans", True, "no cross-region references")


def _fts_smoke(conn: sqlite3.Connection) -> CheckResult:
    """Smoke-test any FTS index; a no-op pass until the FTS index lands (§T31)."""
    fts_tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE '%USING fts%'"
        )
    ]
    if not fts_tables:
        return CheckResult("fts_smoke", True, "no FTS index yet (M2/§T31)")
    try:
        for name in fts_tables:
            conn.execute(f"SELECT COUNT(*) FROM {name} WHERE {name} MATCH ?", ("zzzznomatch",))
    except sqlite3.Error as exc:
        return CheckResult("fts_smoke", False, f"FTS query failed: {exc}")
    return CheckResult("fts_smoke", True, f"{len(fts_tables)} FTS table(s) queryable")


def _golden(conn: sqlite3.Connection, *, expected_schema_version: str | None) -> CheckResult:
    problems: list[str] = []
    for table in ("source_snapshots", "enemies", "stages", "zones"):
        bad = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE server NOT IN ('en', 'cn')"  # noqa: S608 - fixed table names
        ).fetchone()[0]
        if bad:
            problems.append(f"{table}: {bad} row(s) with region outside {sorted(VALID_REGIONS)}")
    if expected_schema_version is not None:
        rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        latest = rows[-1][0] if rows else None
        if latest != expected_schema_version:
            problems.append(f"schema version {latest!r} != expected {expected_schema_version!r}")
    if problems:
        return CheckResult("golden", False, "; ".join(problems))
    return CheckResult("golden", True, "domain invariants hold")


def _schema_version(conn: sqlite3.Connection) -> str | None:
    try:
        rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    except sqlite3.Error:
        return None
    return str(rows[-1][0]) if rows else None


def _safe(name: str, run: Callable[[], CheckResult]) -> CheckResult:
    """Run one check, converting a SQLite error into a failed result.

    Each check is independent: a check that raises (e.g. a dropped critical table
    makes the orphan join fail) becomes its own FAIL rather than aborting the whole
    gate, so the report stays complete and actionable.
    """
    try:
        return run()
    except sqlite3.Error as exc:
        return CheckResult(name, False, f"check error: {exc}")


def validate_database(
    db_path: str | Path,
    *,
    expected_schema_version: str | None = None,
    min_snapshots: int = 1,
) -> ValidationReport:
    """Run the full validation gate against a candidate database (read-only, §V2).

    Returns a :class:`ValidationReport`; a missing or non-SQLite file yields a
    report with ``passed`` false (never an exception), so callers fail closed
    (§V3). ``min_snapshots`` may be 0 for a rebuild that legitimately removes its
    only source (§V20).
    """
    path = Path(db_path)
    try:
        conn = open_read_only(path)
    except DatabaseUnavailable as exc:
        return ValidationReport(
            passed=False,
            schema_version=None,
            checks=(CheckResult("integrity_check", False, f"unreadable database: {exc}"),),
        )
    try:
        checks: list[CheckResult] = [
            _safe("integrity_check", lambda: _integrity_check(conn)),
            _safe("foreign_key_check", lambda: _foreign_key_check(conn)),
            _safe("critical_tables", lambda: _critical_tables(conn)),
            _safe("row_counts", lambda: _row_counts(conn, min_snapshots=min_snapshots)),
            _safe("orphans", lambda: _orphans(conn)),
            _safe("fts_smoke", lambda: _fts_smoke(conn)),
            _safe("golden", lambda: _golden(conn, expected_schema_version=expected_schema_version)),
        ]
        schema_version = _schema_version(conn)
    finally:
        conn.close()
    passed = all(c.passed for c in checks)
    return ValidationReport(passed=passed, schema_version=schema_version, checks=tuple(checks))


def format_report(report: ValidationReport) -> str:
    """Human-readable multi-line rendering for the ``validate`` CLI command."""
    lines = [f"validation: {'PASS' if report.passed else 'FAIL'} (schema {report.schema_version})"]
    for check in report.checks:
        mark = "ok " if check.passed else "FAIL"
        lines.append(f"  [{mark}] {check.name}: {check.detail}")
    return "\n".join(lines)


__all__ = [
    "CheckResult",
    "ValidationReport",
    "validate_database",
    "format_report",
    "CRITICAL_TABLES",
]
