"""Explicit schema migration runner (SPEC §C "small runner"; §T12).

Applies ``migrations/NNNN_*.sql`` files in order against a writable SQLite
candidate database, recording each applied version and its checksum in
``schema_migrations``. Idempotent (already-applied versions are skipped) and
drift-detecting (a changed migration file whose version is already recorded
raises rather than silently diverging). Building always targets a fresh
candidate; a failed migration leaves the candidate to be discarded (§V3).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from arknights_mcp.util.hashing import sha256_hex

# Packaged migrations directory (``src/arknights_mcp/migrations``). Living inside
# the package means the ``.sql`` files ship in the wheel, so a non-editable install
# finds them via ``importlib.resources`` -- not only an editable/source checkout
# (fixes M7). Tests may still pass an explicit directory.
DEFAULT_MIGRATIONS_DIR = Path(str(resources.files("arknights_mcp").joinpath("migrations")))


class MigrationError(RuntimeError):
    """Raised on checksum drift or a failed migration."""


def default_migrations_dir() -> Path:
    return DEFAULT_MIGRATIONS_DIR


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _applied_versions(conn: sqlite3.Connection) -> dict[str, str]:
    if not _table_exists(conn, "schema_migrations"):
        return {}
    return {
        version: checksum
        for version, checksum in conn.execute("SELECT version, checksum FROM schema_migrations")
    }


def _migration_files(migrations_dir: Path) -> list[Path]:
    if not migrations_dir.is_dir():
        raise MigrationError(f"migrations directory not found: {migrations_dir}")
    return sorted(migrations_dir.glob("[0-9]*.sql"))


def apply_migrations(
    conn: sqlite3.Connection,
    migrations_dir: Path | None = None,
) -> list[str]:
    """Apply pending migrations; return the versions newly applied this call."""
    directory = migrations_dir if migrations_dir is not None else default_migrations_dir()
    conn.execute("PRAGMA foreign_keys = ON")
    applied = _applied_versions(conn)
    newly: list[str] = []

    for path in _migration_files(directory):
        version = path.stem  # e.g. "0001_core_metadata"
        sql = path.read_text(encoding="utf-8")
        checksum = sha256_hex(sql.encode("utf-8"))

        if version in applied:
            if applied[version] != checksum:
                raise MigrationError(f"checksum drift for already-applied migration {version!r}")
            continue

        try:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at, checksum) VALUES (?, ?, ?)",
                (version, _now_iso(), checksum),
            )
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise MigrationError(f"migration {version!r} failed: {exc}") from exc
        newly.append(version)

    return newly


def open_writable(db_path: str | Path) -> sqlite3.Connection:
    """Open a writable connection for building a candidate database (FKs on)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def build_database(
    db_path: str | Path,
    migrations_dir: Path | None = None,
) -> sqlite3.Connection:
    """Open a writable candidate DB and apply all migrations."""
    conn = open_writable(db_path)
    apply_migrations(conn, migrations_dir)
    return conn
