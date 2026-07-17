"""Operational source-policy event journal + materialization (§T26; PRD 10.8, 12.2).

``source_policy_events`` is a table inside the *immutable* versioned build, so a
bare ``source enable``/``disable`` -- which keeps the current data and does not
rebuild -- cannot write to it without mutating the active database, which is
forbidden (§V4). Events are therefore appended to an operational journal
(``data/policy_events.jsonl``) the moment the admin acts, and every build
(``sync``/``import``/``purge --rebuild``) *materializes* the full journal into the
candidate's ``source_policy_events`` table. The content database stays immutable;
the policy-event history is durable and identical in every build it produces.

The journal is operational metadata (not game content, not a public interface):
it lives under ``data/`` and is git-ignored. It never records secrets.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: Journal filename under the configured data directory.
POLICY_EVENTS_FILENAME = "policy_events.jsonl"

#: Allowed ``event_type`` values (mirrors the CHECK constraint in migration 0001).
EVENT_TYPES = frozenset({"enable", "disable", "purge", "permission_review", "attribution_change"})


class PolicyEventError(ValueError):
    """Raised for a malformed policy event (unknown type, missing source)."""


@dataclass(frozen=True)
class PolicyEvent:
    """One ``source_policy_events`` row (``event_id`` is DB-assigned)."""

    source_id: str
    event_type: str
    created_at: str
    reason: str | None = None
    actor_id: str | None = None
    result_manifest_hash: str | None = None

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "source_id": self.source_id,
                "event_type": self.event_type,
                "created_at": self.created_at,
                "reason": self.reason,
                "actor_id": self.actor_id,
                "result_manifest_hash": self.result_manifest_hash,
            },
            sort_keys=True,
        )

    @classmethod
    def from_mapping(cls, data: dict[str, object]) -> PolicyEvent:
        event_type = str(data.get("event_type", ""))
        if event_type not in EVENT_TYPES:
            raise PolicyEventError(f"unknown policy event_type: {event_type!r}")
        return cls(
            source_id=str(data["source_id"]),
            event_type=event_type,
            created_at=str(data.get("created_at", "")),
            reason=_opt_str(data.get("reason")),
            actor_id=_opt_str(data.get("actor_id")),
            result_manifest_hash=_opt_str(data.get("result_manifest_hash")),
        )


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def journal_path(data_dir: str | Path) -> Path:
    """Path to the policy-event journal under ``data_dir``."""
    return Path(data_dir) / POLICY_EVENTS_FILENAME


def append_event(
    data_dir: str | Path,
    *,
    source_id: str,
    event_type: str,
    reason: str | None = None,
    actor_id: str | None = None,
    result_manifest_hash: str | None = None,
    created_at: str | None = None,
) -> PolicyEvent:
    """Append one event to the journal (creating ``data_dir`` if needed)."""
    if event_type not in EVENT_TYPES:
        raise PolicyEventError(f"unknown policy event_type: {event_type!r}")
    event = PolicyEvent(
        source_id=source_id,
        event_type=event_type,
        created_at=created_at if created_at is not None else _now_iso(),
        reason=reason,
        actor_id=actor_id,
        result_manifest_hash=result_manifest_hash,
    )
    path = journal_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(event.to_json_line() + "\n")
    return event


def read_events(data_dir: str | Path) -> list[PolicyEvent]:
    """Parse every event in the journal (empty list when the journal is absent)."""
    path = journal_path(data_dir)
    if not path.is_file():
        return []
    events: list[PolicyEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        events.append(PolicyEvent.from_mapping(json.loads(stripped)))
    return events


def _known_source_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT source_id FROM data_sources")}


def materialize_policy_events(
    conn: sqlite3.Connection, events: Sequence[PolicyEvent] | Iterable[PolicyEvent]
) -> int:
    """Rewrite ``source_policy_events`` from ``events`` (idempotent; returns rows written).

    The table is cleared first so a filtered-copy rebuild does not duplicate the
    events it inherited from the source build: after this call the table equals the
    journal exactly. Events referencing a ``source_id`` absent from ``data_sources``
    are skipped (they would violate the foreign key); this can only happen if a
    source is dropped entirely from the registry, which is out of M1 scope.
    """
    known = _known_source_ids(conn)
    conn.execute("DELETE FROM source_policy_events")
    written = 0
    for event in events:
        if event.source_id not in known:
            continue
        conn.execute(
            "INSERT INTO source_policy_events "
            "(source_id, event_type, reason, created_at, actor_id, result_manifest_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                event.source_id,
                event.event_type,
                event.reason,
                event.created_at,
                event.actor_id,
                event.result_manifest_hash,
            ),
        )
        written += 1
    return written
