"""Entity search read repository (§V2; §T31).

The single parameterized SQL surface for the ``search_entities`` service: one FTS5
``MATCH`` query over ``entity_fts`` with optional region (``server``) and
``entity_type`` filters, ranked best-first (bm25 ``rank``) and bounded by an
already-clamped ``limit`` (§V19). Every runtime value -- the MATCH expression,
the filters, the limit -- is bound through ``?`` placeholders; the FTS match
expression is built by the service from tokenized input so no FTS operator or SQL
syntax can be smuggled in (§V2/§V18). Rows come back as flat typed hits carrying
their region (§V5); the service shapes them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arknights_mcp.db.repositories.base import Repository


@dataclass(frozen=True)
class SearchHitRow:
    """One FTS hit: its typed identity + region (§V5) and display fields.

    ``difficulty`` is the stage variant tag (§V70): a stage carries its
    ``stages.difficulty`` value (the same value ``get_stage`` returns), so two
    stages that share a ``display_name`` + ``stage_code`` (a normal stage and its
    challenge variant) are distinguishable in one result set without parsing the
    game-data ``game_id`` suffix (B59). It is ``None`` for a non-stage hit
    (operators/enemies have no difficulty) and for a stage with no difficulty in
    source.
    """

    entity_type: str
    server: str
    entity_pk: int
    game_id: str
    name: str | None
    stage_code: str | None
    difficulty: str | None


# The ``(? IS NULL OR col = ?)`` pairs make server / entity_type optional filters
# while keeping every value bound (no interpolation, §V2). ``ORDER BY rank`` is
# FTS5's bm25 ordering: best match first.
#
# The ``LEFT JOIN stages`` surfaces the §V70 stage variant tag: a stage hit
# carries its ``stages.difficulty`` (``entity_pk`` == ``stage_pk`` for a stage
# document, §T31), so a client can tell a normal stage from its challenge variant
# when both share a display_name + stage_code (B59). The join is guarded by
# ``entity_type = 'stage'`` in the ON clause -- ``stage_pk`` is a separate PK space
# from ``operator_pk`` / ``enemy_pk``, so without the guard an operator/enemy whose
# pk collided with a stage_pk would spuriously borrow a difficulty; with it, every
# non-stage hit gets a ``NULL`` difficulty via the outer join. It is a pure
# additive read (§V21) -- no migration, no FTS schema change; the ambiguous columns
# (server / game_id / stage_code) are qualified to ``entity_fts`` so the join adds
# no interpolation and every value stays bound (§V2).
#
# The locale filter (§V57) is the trailing ``(? IS NULL OR EXISTS ...)`` clause: when
# a locale is bound, a hit survives only if its entity carries an alias tagged with
# that locale in ``operator_aliases`` / ``enemy_aliases`` (the two alias tables that
# carry the §T98 ``locale`` column). Stages have no alias table, so a locale-scoped
# search never returns a stage -- correct, since a stage carries no locale name. The
# ``EXISTS`` sub-selects key on the UNINDEXED ``entity_pk`` + ``entity_type`` so the
# filter is a pure post-match narrowing; every value stays bound (§V2). A ``NULL``
# locale short-circuits the whole clause, so the unfiltered path is byte-unchanged
# (§V21 additive).
_SEARCH_SQL = (
    "SELECT entity_fts.entity_type, entity_fts.server, entity_fts.entity_pk, "
    "entity_fts.game_id, entity_fts.name, entity_fts.stage_code, s.difficulty "
    "FROM entity_fts "
    "LEFT JOIN stages s "
    "ON entity_fts.entity_type = 'stage' "
    "AND s.stage_pk = entity_fts.entity_pk "
    "AND s.server = entity_fts.server "
    "WHERE entity_fts MATCH ? "
    "AND (? IS NULL OR entity_fts.server = ?) "
    "AND (? IS NULL OR entity_fts.entity_type = ?) "
    "AND (? IS NULL OR ("
    "  (entity_fts.entity_type = 'operator' AND EXISTS ("
    "    SELECT 1 FROM operator_aliases oa "
    "    WHERE oa.operator_pk = entity_fts.entity_pk AND oa.locale = ?)) "
    "  OR (entity_fts.entity_type = 'enemy' AND EXISTS ("
    "    SELECT 1 FROM enemy_aliases ea "
    "    WHERE ea.enemy_pk = entity_fts.entity_pk AND ea.locale = ?)) "
    ")) "
    "ORDER BY rank "
    "LIMIT ?"
)

# ``search_stages`` (§T33): stage-scoped FTS, but a stage whose ``stage_code``
# equals the raw query (case-insensitive) is pulled to the top ahead of bm25
# ``rank`` -- an exact code match ("4-4") beats a fuzzier name/game-id hit. The
# exact-code candidate is bound (§V2), never interpolated, and ``rank`` breaks
# ties within each group. The ``LEFT JOIN stages`` surfaces the §V70 difficulty
# variant tag on every stage hit (see ``_SEARCH_SQL``); the WHERE already scopes to
# ``entity_type = 'stage'`` so the join always resolves to the hit's own stage row.
_STAGE_SEARCH_SQL = (
    "SELECT entity_fts.entity_type, entity_fts.server, entity_fts.entity_pk, "
    "entity_fts.game_id, entity_fts.name, entity_fts.stage_code, s.difficulty "
    "FROM entity_fts "
    "LEFT JOIN stages s "
    "ON s.stage_pk = entity_fts.entity_pk "
    "AND s.server = entity_fts.server "
    "WHERE entity_fts MATCH ? "
    "AND entity_fts.entity_type = 'stage' "
    "AND (? IS NULL OR entity_fts.server = ?) "
    "ORDER BY (CASE WHEN entity_fts.stage_code = ? COLLATE NOCASE THEN 0 ELSE 1 END), rank "
    "LIMIT ?"
)


def _to_hit(row: Any) -> SearchHitRow:
    entity_type, server, entity_pk, game_id, name, stage_code, difficulty = row
    return SearchHitRow(
        entity_type=entity_type,
        server=server,
        entity_pk=entity_pk,
        game_id=game_id,
        name=name,
        stage_code=stage_code,
        difficulty=difficulty,
    )


class SearchRepository(Repository):
    """Read-only FTS5 access for entity search (§V2)."""

    def search(
        self,
        match: str,
        *,
        server: str | None,
        entity_type: str | None,
        locale: str | None = None,
        limit: int,
    ) -> list[SearchHitRow]:
        """Return up to ``limit`` ranked hits for the FTS ``match`` expression.

        ``match`` is a pre-built FTS5 expression (already tokenized + quoted by the
        service); ``server`` / ``entity_type`` / ``locale`` are optional filters
        (``None`` = unfiltered). A ``locale`` filter (§V57) keeps only entities
        carrying an alias in that locale (operators/enemies; a stage has no locale
        alias so a locale-scoped search never returns one). ``limit`` is expected
        pre-clamped to the §V19 bound.
        """
        params = (
            match,
            server,
            server,
            entity_type,
            entity_type,
            locale,
            locale,
            locale,
            limit,
        )
        return [_to_hit(r) for r in self._all(_SEARCH_SQL, params)]

    def search_stages(
        self,
        match: str,
        *,
        exact_code: str,
        server: str | None,
        limit: int,
    ) -> list[SearchHitRow]:
        """Return up to ``limit`` stage hits, exact ``stage_code`` first (§T33).

        ``match`` is the pre-built, tokenized FTS expression (same safe surface as
        :meth:`search`); ``exact_code`` is the raw query, compared case-insensitively
        against ``stage_code`` so an exact code match ranks ahead of bm25 ``rank``.
        ``server`` is an optional region filter (§V5); ``limit`` is pre-clamped to
        the §V19 bound. Every value is bound (§V2).
        """
        params = (match, server, server, exact_code, limit)
        return [_to_hit(r) for r in self._all(_STAGE_SEARCH_SQL, params)]
