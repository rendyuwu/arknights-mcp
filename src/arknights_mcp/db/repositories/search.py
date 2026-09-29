"""Entity search read repository.

The single parameterized SQL surface for the ``search_entities`` service: one FTS5
``MATCH`` query over ``entity_fts`` with optional region (``server``) and
``entity_type`` filters, bounded by an already-clamped ``limit``. Membership
in the bounded set is best-match-first (bm25 ``rank``) within the own-name
precedence group (see :func:`_alias_only_group`), so a strong hit in either region is
never evicted by weaker matches from the other; the selected set is then
displayed region-major (en before cn) with deterministic tie-breaks. Every
runtime value -- the MATCH expression, the filters, the limit -- is bound through
``?`` placeholders; the FTS match
expression is built by the service from tokenized input so no FTS operator or SQL
syntax can be smuggled in. Rows come back as flat typed hits carrying
their region; the service shapes them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from arknights_mcp.db.repositories.base import Repository
from arknights_mcp.util.sqlite import column_exists


@dataclass(frozen=True)
class SearchHitRow:
    """One FTS hit: its typed identity + region and display fields.

    ``difficulty`` is the RAW ``stages.difficulty`` column: the search
    service derives the client-facing variant tag from it plus the game_id prefix
    through the one home (``tough_*`` / ``easy_*`` upgraded off
    ``NORMAL``), so the wire value matches ``get_stage``. It is ``None`` for a
    non-stage hit (operators/enemies have no difficulty) and for a stage with no
    difficulty in source.

    ``zone_display_name`` is the stage's zone display name: the
    string that made an alias-only hit match, so the client can tell *why* a stage
    it never named came back and partition mixed hits by event. ``None`` for a
    non-stage hit and for a stage whose zone carries no name in source (416 of 3264
    en stages on the 2026-07-27 build) -- the tool layer omits the key there.

    ``event_name`` is the stage's EVENT TITLE (migration 0015) and is a
    different fact from a different file: ``zone_display_name`` is the sub-zone
    subtitle from ``zone_table`` ("The Coming of The Future"), ``event_name`` the
    title from ``activity_table`` ("Lone Trail"). Either can be the string that made
    an alias-only hit match, so both ride the row rather than one conflated column.
    ``None`` for a non-stage hit, for a zone with no activity row (annihilation /
    tower / IS zones), and on an ACTIVE build predating migration 0015.
    """

    entity_type: str
    server: str
    entity_pk: int
    game_id: str
    name: str | None
    stage_code: str | None
    difficulty: str | None
    zone_display_name: str | None
    event_name: str | None


# The deterministic region order (en before cn), as a build-time SQL fragment
# parameterized ONLY by a literal column reference we author -- one home so the
# two queries below can never drift on region order. No runtime value is ever
# interpolated.
def _region_order(server_col: str) -> str:
    return f"(CASE WHEN {server_col} = 'en' THEN 0 ELSE 1 END)"


# Own-name precedence. The ``aliases`` FTS column means two different
# things depending on the document's entity_type, and that conflation is the bug:
#
# * operator / enemy -> the entity's OWN name in another language (878 cn operators on
#   the 2026-07-27 build are reachable by their EN name ONLY through this column);
# * stage -> its ZONE's display name, which is another entity's name entirely.
#
# So an unscoped bm25 lets a whole event's member stages compete with the operator that
# shares its name inside the bounded top-N: real corpus, query "Gavial", limit 10 ->
# seven "Gavial's Footprints" stages crowded out both the stage actually NAMED
# "Gavial's Fist" and the cn operator (they landed at #14 and #17).
#
# A per-column weight alone cannot fix it: down-weighting ``aliases`` globally also
# demotes the cross-language operator lookup (measured -- the cn operator stayed at
# #17), and any weight only makes the collision unlikely, never impossible, which is
# weaker than the absolute "alias must not outrank an entity's own name".
#
# The probe below is exact instead. bm25 sums per-column contributions, so evaluating it
# with the ``aliases`` weight set to 0.0 returns exactly 0.0 (SQLite emits ``-0.0``, and
# ``= 0.0`` matches it) for a document that matched through NO other column -- i.e. an
# alias-only hit -- and a real negative score for anything that also matched its own
# name / game_id / stage_code / tags. Grouping on that gives a hard precedence tier by
# construction, for any query, not just the collisions we happened to count. Scoped to
# ``entity_type = 'stage'`` because only there does ``aliases`` hold a foreign name.
# The column weights are literals we author, never runtime values.
_ALIAS_ONLY_GROUP = (
    "(CASE WHEN entity_fts.entity_type = 'stage' "
    "AND bm25(entity_fts, 1.0, 1.0, 0.0, 1.0, 1.0) = 0.0 THEN 1 ELSE 0 END)"
)


# The ``(? IS NULL OR col = ?)`` pairs make server / entity_type optional filters
# while keeping every value bound (no interpolation).
#
# MEMBERSHIP vs DISPLAY: the inner query selects the top-``limit`` hits by bm25
# ``rank`` ALONE (ties broken by the region CASE then ``game_id``, so the boundary is
# deterministic) -- a strong cn match is never evicted from the bounded result set by
# a pile of weaker en matches. The outer query then DISPLAYS that set region-major
# (every en hit before any cn hit, then rank, then ``game_id`` for stable ties), which
# is the deterministic ``results[0]`` contract: twin en/cn documents have
# equal bm25 rank, so the en twin deterministically leads. Both CASEs compare against
# the constant literal ``'en'`` only.
#
# The ``LEFT JOIN stages`` surfaces the stage variant tag: a stage hit
# carries its ``stages.difficulty`` (``entity_pk`` == ``stage_pk`` for a stage
# document), so a client can tell a normal stage from its challenge variant
# when both share a display_name + stage_code. The join is guarded by
# ``entity_type = 'stage'`` in the ON clause -- ``stage_pk`` is a separate PK space
# from ``operator_pk`` / ``enemy_pk``, so without the guard an operator/enemy whose
# pk collided with a stage_pk would spuriously borrow a difficulty; with it, every
# non-stage hit gets a ``NULL`` difficulty via the outer join. It is a pure
# additive read -- no migration, no FTS schema change; the ambiguous columns
# (server / game_id / stage_code) are qualified to ``entity_fts`` so the join adds
# no interpolation and every value stays bound.
#
# The chained ``LEFT JOIN zones`` surfaces the zone/event attribution: a stage that
# matched through its zone/event name carries that name on the wire, so a client can see
# why a stage it never named came back and can partition mixed hits by event. Region-
# guarded (``z.server = s.server``) in parity with the search-index join, so a stage can
# never borrow the other region's zone name. Additive read -- no migration,
# no FTS schema change, and it rides the ``stages`` join already present above.
#
# ``alias_grp`` (:data:`_ALIAS_ONLY_GROUP`) leads BOTH orderings: membership first, so an
# alias-only stage can never evict an own-name entity from the bounded set, then display,
# so it never outranks one in the returned list either.
#
# The extra-locale (ja/ko) NAME-alias filter is RETIRED: there is no
# trailing locale ``EXISTS`` clause and the alias tables are no longer consulted at
# query time. Aliases still feed the FTS ``name`` document at build time (operator
# self-aliases), so an operator remains matchable by appellation.
# The event-title expression is the one part of these queries that varies, and it
# varies on the SCHEMA, never on a request: ``zones.event_name`` arrives in migration
# 0015, and the server may be serving an ACTIVE build made before it. Both
# alternatives are literals authored here and the pair is expanded at import time, so
# the executed SQL is still a fixed constant chosen from a closed set -- nothing from a
# caller reaches the query text.
_EVENT_NAME_COLUMN = "z.event_name"
_EVENT_NAME_ABSENT = "NULL"
_EVENT_NAME_EXPRESSIONS = (_EVENT_NAME_COLUMN, _EVENT_NAME_ABSENT)


def _build_search_sql(event_name: str) -> str:
    return (
        "SELECT entity_type, server, entity_pk, game_id, name, stage_code, difficulty, "
        "zone_display_name, event_name FROM ("
        "SELECT entity_fts.entity_type, entity_fts.server, entity_fts.entity_pk, "
        "entity_fts.game_id, entity_fts.name, entity_fts.stage_code, s.difficulty, "
        "z.display_name AS zone_display_name, "
        f"{event_name} AS event_name, "
        f"{_ALIAS_ONLY_GROUP} AS alias_grp, "
        "rank AS score "
        "FROM entity_fts "
        "LEFT JOIN stages s "
        "ON entity_fts.entity_type = 'stage' "
        "AND s.stage_pk = entity_fts.entity_pk "
        "AND s.server = entity_fts.server "
        "LEFT JOIN zones z ON z.zone_pk = s.zone_pk AND z.server = s.server "
        "WHERE entity_fts MATCH ? "
        "AND (? IS NULL OR entity_fts.server = ?) "
        "AND (? IS NULL OR entity_fts.entity_type = ?) "
        f"ORDER BY alias_grp, rank, {_region_order('entity_fts.server')}, entity_fts.game_id "
        "LIMIT ?"
        ") "
        f"ORDER BY alias_grp, {_region_order('server')}, score, game_id"
    )


_SEARCH_SQL = {expr: _build_search_sql(expr) for expr in _EVENT_NAME_EXPRESSIONS}


# ``search_stages``: stage-scoped FTS, but a stage whose ``stage_code``
# equals the raw query (case-insensitive) is pulled to the top ahead of bm25
# ``rank`` -- an exact code match ("4-4") beats a fuzzier name/game-id hit, in BOTH
# the membership cut and the display order. The exact-code candidate is bound,
# never interpolated. Membership within the exact/non-exact groups is bm25-first with
# the deterministic region/game_id tie-break; display within each group is
# region-major (en before cn) then rank (an unfiltered "1-7" must not surface
# the cn row first). The ``LEFT JOIN stages`` surfaces the difficulty variant
# tag on every stage hit (see ``_SEARCH_SQL``); the WHERE already scopes to
# ``entity_type = 'stage'`` so the join always resolves to the hit's own stage row,
# and the chained ``zones`` join carries the zone/event attribution.
#
# ``alias_grp`` sits BELOW ``exact_grp`` and above rank: an exact stage-code match still
# wins outright (the exact-match contract), then a stage matching on its own name/code outranks
# one matching only through its zone name, in both membership and display.
def _build_stage_search_sql(event_name: str) -> str:
    return (
        "SELECT entity_type, server, entity_pk, game_id, name, stage_code, difficulty, "
        "zone_display_name, event_name FROM ("
        "SELECT entity_fts.entity_type, entity_fts.server, entity_fts.entity_pk, "
        "entity_fts.game_id, entity_fts.name, entity_fts.stage_code, s.difficulty, "
        "z.display_name AS zone_display_name, "
        f"{event_name} AS event_name, "
        "(CASE WHEN entity_fts.stage_code = ? COLLATE NOCASE THEN 0 ELSE 1 END) AS exact_grp, "
        f"{_ALIAS_ONLY_GROUP} AS alias_grp, "
        "rank AS score "
        "FROM entity_fts "
        "LEFT JOIN stages s "
        "ON s.stage_pk = entity_fts.entity_pk "
        "AND s.server = entity_fts.server "
        "LEFT JOIN zones z ON z.zone_pk = s.zone_pk AND z.server = s.server "
        "WHERE entity_fts MATCH ? "
        "AND entity_fts.entity_type = 'stage' "
        "AND (? IS NULL OR entity_fts.server = ?) "
        "ORDER BY exact_grp, alias_grp, rank, "
        f"{_region_order('entity_fts.server')}, entity_fts.game_id "
        "LIMIT ?"
        ") "
        f"ORDER BY exact_grp, alias_grp, {_region_order('server')}, score, game_id"
    )


_STAGE_SEARCH_SQL = {expr: _build_stage_search_sql(expr) for expr in _EVENT_NAME_EXPRESSIONS}


def _to_hit(row: Any) -> SearchHitRow:
    (
        entity_type,
        server,
        entity_pk,
        game_id,
        name,
        stage_code,
        difficulty,
        zone_display_name,
        event_name,
    ) = row
    return SearchHitRow(
        entity_type=entity_type,
        server=server,
        entity_pk=entity_pk,
        game_id=game_id,
        name=name,
        stage_code=stage_code,
        difficulty=difficulty,
        zone_display_name=zone_display_name,
        event_name=event_name,
    )


class SearchRepository(Repository):
    """Read-only FTS5 access for entity search."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__(conn)
        # Probed once per repository, not per query: the schema cannot change under a
        # read-only connection to an immutable build.
        self._event_name = (
            _EVENT_NAME_COLUMN if column_exists(conn, "zones", "event_name") else _EVENT_NAME_ABSENT
        )

    def search(
        self,
        match: str,
        *,
        server: str | None,
        entity_type: str | None,
        limit: int,
    ) -> list[SearchHitRow]:
        """Return up to ``limit`` ranked hits for the FTS ``match`` expression.

        ``match`` is a pre-built FTS5 expression (already tokenized + quoted by the
        service); ``server`` / ``entity_type`` are optional filters (``None`` =
        unfiltered). ``limit`` is expected pre-clamped to the bound. A stage that
        matched only through its zone/event name ranks below every own-name hit and so
        can never evict one from the bounded set (:data:`_ALIAS_ONLY_GROUP`). The
        extra-locale (ja/ko) alias filter is retired.
        """
        params = (match, server, server, entity_type, entity_type, limit)
        return [_to_hit(r) for r in self._all(_SEARCH_SQL[self._event_name], params)]

    def search_stages(
        self,
        match: str,
        *,
        exact_code: str,
        server: str | None,
        limit: int,
    ) -> list[SearchHitRow]:
        """Return up to ``limit`` stage hits, exact ``stage_code`` first.

        ``match`` is the pre-built, tokenized FTS expression (same safe surface as
        :meth:`search`); ``exact_code`` is the raw query, compared case-insensitively
        against ``stage_code`` so an exact code match ranks ahead of bm25 ``rank``.
        Below that, a stage matching only through its zone/event name ranks under one
        matching its own name or code (:data:`_ALIAS_ONLY_GROUP`). ``server`` is
        an optional region filter; ``limit`` is pre-clamped to the bound.
        Every value is bound.
        """
        params = (exact_code, match, server, server, limit)
        return [_to_hit(r) for r in self._all(_STAGE_SEARCH_SQL[self._event_name], params)]
