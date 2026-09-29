"""Build the unified entity search index.

The single home that populates the ``entity_fts`` FTS5 index from the
already-imported base tables. It runs once at the end of a candidate build
(:mod:`arknights_mcp.importers.pipeline`), on the writable candidate, so an MCP
process only ever reads the index. Each indexed document carries its typed
identity (``entity_type`` + ``server`` + ``entity_pk``) plus the searchable
columns (``game_id`` + ``name`` + ``aliases`` + ``stage_code`` + ``tags``).

Sources per entity type:

* enemy  -> ``enemies`` (name) + ``enemy_aliases`` (aliases);
* stage  -> ``stages`` (name + ``stage_code``) + its zone SUBTITLE *and* event TITLE
  from ``zones`` as the stage's alias ("Lone Trail" finds the stages in that event,
  and the title comes from ``activity_table``, not the zone name; the zone itself is
  not a queryable entity, so the names ride the stage documents instead of a zone
  document);
* operator -> ``operators`` (name + ``tag_json`` -> tags) + ``operator_aliases``;
* item -> ``items`` (name; no alias/code/tag table).

Only entity types actually present contribute rows, so this is a no-op for the
domains not yet imported (operators land in M4); the index simply grows with the
data (one code path, no per-domain divergence).
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
# database_hash -> the "unchanged -> no-op" promotion (and reproducibility).
_ENEMY_SQL = (
    "SELECT e.enemy_pk, e.server, e.game_id, e.display_name, "
    "(SELECT GROUP_CONCAT(a.alias, ' ' ORDER BY a.alias) FROM enemy_aliases a "
    "WHERE a.enemy_pk = e.enemy_pk) "
    "FROM enemies e"
)
# A stage's alias is its zone/event name: one zone per stage via the zone_pk
# FK, so no GROUP_CONCAT and no ordering concern -- the document bytes stay
# deterministic. The join is region-guarded (z.server = s.server) so a stage can
# never borrow a display name from the other region's zone row.
# ORDER BY pins the stage enumeration (and thus FTS document insert order /
# rowids) to the table's own key rather than a query-planner scan artifact, so
# two builds of byte-identical source stay byte-identical (the determinism class).
#
# TWO names come back, because the zone holds two. ``z.display_name`` is
# the sub-zone SUBTITLE zone_table carries ("The Coming of The Future"); ``z.event_name``
# is the event TITLE from activity_table ("Lone Trail") -- the name a client actually
# types, and one that appears nowhere in zone_table. Indexing only the first is the bug:
# the promised "an event name finds that event's stages" matched nothing for 120 EN
# events. Both go in the SAME ``aliases`` column, so the own-name precedence tier
# (which keys on that column being the only match) is unchanged.
_STAGE_SQL = (
    "SELECT s.stage_pk, s.server, s.game_id, s.display_name, s.stage_code, "
    "z.display_name, z.event_name "
    "FROM stages s "
    "LEFT JOIN zones z ON z.zone_pk = s.zone_pk AND z.server = s.server "
    "ORDER BY s.stage_pk"
)
_OPERATOR_SQL = (
    "SELECT o.operator_pk, o.server, o.game_id, o.display_name, o.tag_json, "
    "(SELECT GROUP_CONCAT(a.alias, ' ' ORDER BY a.alias) FROM operator_aliases a "
    "WHERE a.operator_pk = o.operator_pk) "
    "FROM operators o"
)
# Items are name-only: no alias/stage_code/tag table, so no GROUP_CONCAT (the
# ORDER-BY concern does not arise). Indexing the item domain makes an
# item resolvable by name -> game_id in ``search_entities``, so ``get_item_drops``
# has a real name->id path instead of the dead-end pointer.
_ITEM_SQL = "SELECT i.item_pk, i.server, i.game_id, i.display_name FROM items i"


def _stage_aliases(zone_name: str | None, event_name: str | None) -> str | None:
    """Join a stage's zone SUBTITLE and event TITLE into its alias document.

    Order is fixed (subtitle then title) and an exact duplicate is dropped -- some
    events name a zone after themselves -- so two builds of byte-identical source
    produce byte-identical FTS documents (the determinism concern). ``None`` when
    the zone carries neither name (44 EN zones have no activity row and no subtitle).
    """
    parts = [part for part in (zone_name, event_name) if part]
    unique = list(dict.fromkeys(parts))
    return " ".join(unique) if unique else None


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

    for stage_pk, server, game_id, name, stage_code, zone_name, event_name in conn.execute(
        _STAGE_SQL
    ):
        aliases = _stage_aliases(zone_name, event_name)
        rows.append(("stage", server, stage_pk, game_id, name, aliases, stage_code, None))

    for operator_pk, server, game_id, name, tag_json, aliases in conn.execute(_OPERATOR_SQL):
        tags = _tags_from_json(tag_json)
        rows.append(("operator", server, operator_pk, game_id, name, aliases, None, tags))

    for item_pk, server, game_id, name in conn.execute(_ITEM_SQL):
        rows.append(("item", server, item_pk, game_id, name, None, None, None))

    conn.executemany(_INSERT_SQL, rows)
    return len(rows)


def rebuild_search_index(conn: sqlite3.Connection) -> int:
    """Clear and repopulate ``entity_fts`` so it matches the current base tables.

    ``entity_fts`` is a *standalone* FTS5 index with no triggers (migration 0007):
    a build is immutable once promoted, so nothing tracks base-table edits. A
    filtered purge (:mod:`arknights_mcp.db.purge`) *does* delete base rows in place
    on the candidate, which would leave the purged source's documents behind and
    let a taken-down entity keep surfacing in search. Dropping and
    rebuilding the whole index keeps it consistent with the surviving rows -- and
    FTS population stays a single home rather than a per-domain delete fork.
    """
    conn.execute("DELETE FROM entity_fts")
    return build_search_index(conn)
