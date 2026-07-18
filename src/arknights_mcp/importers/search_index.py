"""Build the unified entity search index (SPEC §T31).

The single home (§V37) that populates the ``entity_fts`` FTS5 index from the
already-imported base tables. It runs once at the end of a candidate build
(:mod:`arknights_mcp.importers.pipeline`), on the writable candidate, so an MCP
process only ever reads the index (§V2). Each indexed document carries its typed
identity (``entity_type`` + ``server`` + ``entity_pk``) plus the §T31 searchable
columns (``game_id`` + ``name`` + ``aliases`` + ``stage_code`` + ``tags``).

Sources per entity type:

* enemy  -> ``enemies`` (name) + ``enemy_aliases`` (aliases);
* stage  -> ``stages`` (name + ``stage_code``);
* operator -> ``operators`` (name + ``tag_json`` -> tags) + ``operator_aliases``.

Only entity types actually present contribute rows, so this is a no-op for the
domains not yet imported (operators land in M4); the index simply grows with the
data (§V37 -- one code path, no per-domain divergence).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

_INSERT_SQL = (
    "INSERT INTO entity_fts "
    "(entity_type, server, entity_pk, game_id, name, aliases, stage_code, tags) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

# Enemies + operators aggregate their aliases via a correlated GROUP_CONCAT so one
# document holds every alias for the entity; stages have no alias table. The
# ``ORDER BY a.alias`` pins the concat order -- SQLite's default GROUP_CONCAT order
# is arbitrary, so two builds of byte-identical source could otherwise emit the
# aliases in different orders, diverging the FTS document bytes -> the file-level
# database_hash -> T24's "unchanged -> no-op" promotion (and reproducibility).
_ENEMY_SQL = (
    "SELECT e.enemy_pk, e.server, e.game_id, e.display_name, "
    "(SELECT GROUP_CONCAT(a.alias, ' ' ORDER BY a.alias) FROM enemy_aliases a "
    "WHERE a.enemy_pk = e.enemy_pk) "
    "FROM enemies e"
)
_STAGE_SQL = "SELECT s.stage_pk, s.server, s.game_id, s.display_name, s.stage_code FROM stages s"
_OPERATOR_SQL = (
    "SELECT o.operator_pk, o.server, o.game_id, o.display_name, o.tag_json, "
    "(SELECT GROUP_CONCAT(a.alias, ' ' ORDER BY a.alias) FROM operator_aliases a "
    "WHERE a.operator_pk = o.operator_pk) "
    "FROM operators o"
)


def _tags_from_json(raw: str | None) -> str | None:
    """Flatten an operator ``tag_json`` list into a space-joined tag string."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    tags = [str(t) for t in data if t is not None]
    return " ".join(tags) if tags else None


def build_search_index(conn: sqlite3.Connection) -> int:
    """Populate ``entity_fts`` from the imported base tables; return rows indexed.

    Idempotent per build: it is called once against a fresh candidate whose index
    starts empty. Read the base tables, write one FTS document per entity.
    """
    rows: list[tuple[Any, ...]] = []

    for enemy_pk, server, game_id, name, aliases in conn.execute(_ENEMY_SQL):
        rows.append(("enemy", server, enemy_pk, game_id, name, aliases, None, None))

    for stage_pk, server, game_id, name, stage_code in conn.execute(_STAGE_SQL):
        rows.append(("stage", server, stage_pk, game_id, name, None, stage_code, None))

    for operator_pk, server, game_id, name, tag_json, aliases in conn.execute(_OPERATOR_SQL):
        tags = _tags_from_json(tag_json)
        rows.append(("operator", server, operator_pk, game_id, name, aliases, None, tags))

    conn.executemany(_INSERT_SQL, rows)
    return len(rows)


def rebuild_search_index(conn: sqlite3.Connection) -> int:
    """Clear and repopulate ``entity_fts`` so it matches the current base tables.

    ``entity_fts`` is a *standalone* FTS5 index with no triggers (migration 0007):
    a build is immutable once promoted, so nothing tracks base-table edits. A
    filtered purge (:mod:`arknights_mcp.db.purge`) *does* delete base rows in place
    on the candidate, which would leave the purged source's documents behind and
    let a taken-down entity keep surfacing in search (§V16/§V20). Dropping and
    rebuilding the whole index keeps it consistent with the surviving rows -- and
    FTS population stays a single home (§V37) rather than a per-domain delete fork.
    """
    conn.execute("DELETE FROM entity_fts")
    return build_search_index(conn)
