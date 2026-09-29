"""Personal account roster store (ADR 0020).

One PostgreSQL table, ``account_roster``: one row per server holding the whole
allowlisted roster as canonical JSON. ``account sync`` replaces that row inside
one transaction, so a reader sees the old or the new roster, never a mix, and a
failed write leaves the old row. MCP reads it on each call through a SELECT-only
role; only CLI ``account sync|purge`` write.

SQL comes only from SQLAlchemy Core constructs with bound parameters. The URL in
``ARKNIGHTS_MCP_ACCOUNT_DB_URL`` is a secret: no error raised here carries the URL,
host, user, password, or a driver message -- only the exception class name.
``sqlite:///`` URLs are accepted for the test suite.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    false,
    insert,
    inspect,
    select,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from arknights_mcp.config import ENV_ACCOUNT_DB_URL
from arknights_mcp.importers.account import (
    ACCOUNT_TRANSFORM_VERSION,
    AccountRoster,
    OwnedModule,
    OwnedOperator,
    OwnedSkill,
)
from arknights_mcp.util.hashing import canonical_json, record_hash

ACCOUNT_SCHEMA_VERSION = "1"
ACCOUNT_SERVER = "en"
_SUPPORTED_DRIVERS = frozenset({"postgresql+pg8000", "sqlite"})
_CONNECT_TIMEOUT_S = 5

_METADATA = MetaData()

ACCOUNT_ROSTER = Table(
    "account_roster",
    _METADATA,
    Column("server", String(8), primary_key=True),
    Column("snapshot_id", String(64), nullable=False),
    Column("schema_version", String(16), nullable=False),
    Column("transform_version", String(16), nullable=False),
    Column("synced_at", String(32), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("roster_json", Text, nullable=False),
)


class AccountStoreError(Exception):
    """Account database failure; the message never carries the URL or a driver message."""


@dataclass(frozen=True)
class StoredRoster:
    snapshot_id: str
    schema_version: str
    transform_version: str
    synced_at: str
    content_hash: str
    #: ``None`` when ``schema_version`` differs (the JSON is then not decoded).
    roster: AccountRoster | None


def roster_to_json(roster: AccountRoster) -> str:
    return canonical_json(dataclasses.asdict(roster)).decode("utf-8")


def roster_from_json(text: str) -> AccountRoster:
    try:
        raw = json.loads(text)
        return AccountRoster(
            lmd=int(raw["lmd"]),
            operators=tuple(
                OwnedOperator(
                    char_id=op["char_id"],
                    elite=op["elite"],
                    level=op["level"],
                    potential=op["potential"],
                    skill_level=op["skill_level"],
                    current_skin_id=op["current_skin_id"],
                    equipped_module_id=op["equipped_module_id"],
                    skills=tuple(OwnedSkill(**s) for s in op["skills"]),
                    modules=tuple(OwnedModule(**m) for m in op["modules"]),
                )
                for op in raw["operators"]
            ),
            skins=tuple(raw["skins"]),
            inventory=tuple((str(item), int(count)) for item, count in raw["inventory"]),
            skipped=int(raw["skipped"]),
        )
    except (KeyError, TypeError, ValueError):
        raise AccountStoreError(
            "the stored account roster is malformed; run `arknights-mcp account sync` again"
        ) from None


class AccountStore:
    def __init__(self, url: str) -> None:
        try:
            parsed = make_url(url)
        except ArgumentError:
            raise AccountStoreError(f"{ENV_ACCOUNT_DB_URL} is not a valid database URL") from None
        if parsed.drivername == "postgresql":
            parsed = parsed.set(drivername="postgresql+pg8000")
        if parsed.drivername not in _SUPPORTED_DRIVERS:
            raise AccountStoreError(
                f"{ENV_ACCOUNT_DB_URL} must be a postgresql:// URL "
                "(sqlite:/// is accepted for tests)"
            )
        # Lazy: no connection is opened here.
        self._engine = create_engine(
            parsed, pool_pre_ping=True, connect_args={"timeout": _CONNECT_TIMEOUT_S}
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AccountStore | None:
        url = env.get(ENV_ACCOUNT_DB_URL, "").strip()
        return cls(url) if url else None

    def load(self, server: str) -> StoredRoster | None:
        # ponytail: decodes the whole roster (~400 operators) per call; cache by
        # snapshot_id if a profile ever shows it
        try:
            with self._engine.connect() as conn:
                # Nothing synced yet: the reader role cannot create the table.
                if not inspect(conn).has_table(ACCOUNT_ROSTER.name):
                    return None
                row = conn.execute(
                    select(ACCOUNT_ROSTER).where(ACCOUNT_ROSTER.c.server == server)
                ).one_or_none()
        except (SQLAlchemyError, OSError) as exc:
            raise AccountStoreError(
                f"the account database is unavailable ({type(exc).__name__})"
            ) from None
        if row is None:
            return None
        current = row.schema_version == ACCOUNT_SCHEMA_VERSION
        return StoredRoster(
            snapshot_id=row.snapshot_id,
            schema_version=row.schema_version,
            transform_version=row.transform_version,
            synced_at=row.synced_at,
            content_hash=row.content_hash,
            roster=roster_from_json(row.roster_json) if current else None,
        )

    def check_writable(self) -> None:
        """Prove the database is reachable and writable; run before any Yostar request.

        The zero-row ``DELETE`` fails for the reader role (read-only transaction,
        no ``DELETE`` grant) and for an unreachable server.
        """
        try:
            with self._engine.begin() as conn:
                _METADATA.create_all(conn)
                conn.execute(delete(ACCOUNT_ROSTER).where(false()))
        except (SQLAlchemyError, OSError) as exc:
            raise AccountStoreError(
                f"the account database in {ENV_ACCOUNT_DB_URL} is unreachable or read-only "
                f"({type(exc).__name__}); nothing was sent"
            ) from None

    def write_roster(self, roster: AccountRoster, *, synced_at: datetime) -> str:
        """Replace the server's row in one transaction; return its ``snapshot_id``."""
        at = synced_at.astimezone(UTC)
        snapshot_id = f"yostar-{ACCOUNT_SERVER}-{at:%Y%m%dT%H%M%SZ}"
        try:
            with self._engine.begin() as conn:
                _METADATA.create_all(conn)
                conn.execute(
                    delete(ACCOUNT_ROSTER).where(ACCOUNT_ROSTER.c.server == ACCOUNT_SERVER)
                )
                conn.execute(
                    insert(ACCOUNT_ROSTER).values(
                        server=ACCOUNT_SERVER,
                        snapshot_id=snapshot_id,
                        schema_version=ACCOUNT_SCHEMA_VERSION,
                        transform_version=ACCOUNT_TRANSFORM_VERSION,
                        synced_at=at.isoformat(timespec="seconds"),
                        content_hash=record_hash(dataclasses.asdict(roster)),
                        roster_json=roster_to_json(roster),
                    )
                )
        except (SQLAlchemyError, OSError) as exc:
            raise AccountStoreError(
                f"could not write the account roster ({type(exc).__name__})"
            ) from None
        return snapshot_id

    def purge(self) -> bool:
        """Delete the server's row; ``True`` when one existed."""
        try:
            with self._engine.begin() as conn:
                _METADATA.create_all(conn)
                result = conn.execute(
                    delete(ACCOUNT_ROSTER).where(ACCOUNT_ROSTER.c.server == ACCOUNT_SERVER)
                )
        except (SQLAlchemyError, OSError) as exc:
            raise AccountStoreError(
                f"could not delete the account roster ({type(exc).__name__})"
            ) from None
        return result.rowcount > 0
