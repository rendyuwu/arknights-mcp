"""Entity search read repository (§V2; §T31).

The single parameterized SQL surface for the ``search_entities`` service: one FTS5
``MATCH`` query over ``entity_fts`` with optional region (``server``) and
``entity_type`` filters, bounded by an already-clamped ``limit`` (§V19). Membership
in the bounded set is best-match-first (bm25 ``rank``) so a strong hit in either
region is never evicted by weaker matches from the other; the selected set is then
displayed region-major (en before cn, B97) with deterministic tie-breaks. Every
runtime value -- the MATCH expression, the filters, the limit -- is bound through
``?`` placeholders; the FTS match
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

    ``difficulty`` is the RAW ``stages.difficulty`` column (§V70): the search
    service derives the client-facing variant tag from it plus the game_id prefix
    through the one §V37 home (§V80/B84 -- ``tough_*`` / ``easy_*`` upgraded off
    ``NORMAL``), so the wire value matches ``get_stage``. It is ``None`` for a
    non-stage hit (operators/enemies have no difficulty) and for a stage with no
    difficulty in source.
    """

    entity_type: str
    server: str
    entity_pk: int
    game_id: str
    name: str | None
    stage_code: str | None
    difficulty: str | None


# The B97 deterministic region order (en before cn), as a build-time SQL fragment
# parameterized ONLY by a literal column reference we author -- one §V37 home so the
# two queries below can never drift on region order. No runtime value is ever
# interpolated (§V2).
def _region_order(server_col: str) -> str:
    return f"(CASE WHEN {server_col} = 'en' THEN 0 ELSE 1 END)"


# The ``(? IS NULL OR col = ?)`` pairs make server / entity_type optional filters
# while keeping every value bound (no interpolation, §V2).
#
# MEMBERSHIP vs DISPLAY (B97): the inner query selects the top-``limit`` hits by bm25
# ``rank`` ALONE (ties broken by the region CASE then ``game_id``, so the boundary is
# deterministic) -- a strong cn match is never evicted from the bounded result set by
# a pile of weaker en matches. The outer query then DISPLAYS that set region-major
# (every en hit before any cn hit, then rank, then ``game_id`` for stable ties), which
# is the deterministic ``results[0]`` contract B97 wanted: twin en/cn documents have
# equal bm25 rank, so the en twin deterministically leads. Both CASEs compare against
# the constant literal ``'en'`` only (§V2).
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
# The extra-locale (ja/ko) NAME-alias filter is RETIRED (§V57, T156): there is no
# trailing locale ``EXISTS`` clause and the alias tables are no longer consulted at
# query time. Aliases still feed the FTS ``name`` document at build time (operator
# self-aliases, §T98), so an operator remains matchable by appellation.
_SEARCH_SQL = (
    "SELECT entity_type, server, entity_pk, game_id, name, stage_code, difficulty FROM ("
    "SELECT entity_fts.entity_type, entity_fts.server, entity_fts.entity_pk, "
    "entity_fts.game_id, entity_fts.name, entity_fts.stage_code, s.difficulty, "
    "rank AS score "
    "FROM entity_fts "
    "LEFT JOIN stages s "
    "ON entity_fts.entity_type = 'stage' "
    "AND s.stage_pk = entity_fts.entity_pk "
    "AND s.server = entity_fts.server "
    "WHERE entity_fts MATCH ? "
    "AND (? IS NULL OR entity_fts.server = ?) "
    "AND (? IS NULL OR entity_fts.entity_type = ?) "
    f"ORDER BY rank, {_region_order('entity_fts.server')}, entity_fts.game_id "
    "LIMIT ?"
    ") "
    f"ORDER BY {_region_order('server')}, score, game_id"
)

# ``search_stages`` (§T33): stage-scoped FTS, but a stage whose ``stage_code``
# equals the raw query (case-insensitive) is pulled to the top ahead of bm25
# ``rank`` -- an exact code match ("4-4") beats a fuzzier name/game-id hit, in BOTH
# the membership cut and the display order. The exact-code candidate is bound (§V2),
# never interpolated. Membership within the exact/non-exact groups is bm25-first with
# the deterministic region/game_id tie-break; display within each group is
# region-major (en before cn) then rank (B97 -- an unfiltered "1-7" must not surface
# the cn row first). The ``LEFT JOIN stages`` surfaces the §V70 difficulty variant
# tag on every stage hit (see ``_SEARCH_SQL``); the WHERE already scopes to
# ``entity_type = 'stage'`` so the join always resolves to the hit's own stage row.
_STAGE_SEARCH_SQL = (
    "SELECT entity_type, server, entity_pk, game_id, name, stage_code, difficulty FROM ("
    "SELECT entity_fts.entity_type, entity_fts.server, entity_fts.entity_pk, "
    "entity_fts.game_id, entity_fts.name, entity_fts.stage_code, s.difficulty, "
    "(CASE WHEN entity_fts.stage_code = ? COLLATE NOCASE THEN 0 ELSE 1 END) AS exact_grp, "
    "rank AS score "
    "FROM entity_fts "
    "LEFT JOIN stages s "
    "ON s.stage_pk = entity_fts.entity_pk "
    "AND s.server = entity_fts.server "
    "WHERE entity_fts MATCH ? "
    "AND entity_fts.entity_type = 'stage' "
    "AND (? IS NULL OR entity_fts.server = ?) "
    f"ORDER BY exact_grp, rank, {_region_order('entity_fts.server')}, entity_fts.game_id "
    "LIMIT ?"
    ") "
    f"ORDER BY exact_grp, {_region_order('server')}, score, game_id"
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
        limit: int,
    ) -> list[SearchHitRow]:
        """Return up to ``limit`` ranked hits for the FTS ``match`` expression.

        ``match`` is a pre-built FTS5 expression (already tokenized + quoted by the
        service); ``server`` / ``entity_type`` are optional filters (``None`` =
        unfiltered). ``limit`` is expected pre-clamped to the §V19 bound. The
        extra-locale (ja/ko) alias filter is retired (§V57, T156).
        """
        params = (match, server, server, entity_type, entity_type, limit)
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
        params = (exact_code, match, server, server, limit)
        return [_to_hit(r) for r in self._all(_STAGE_SEARCH_SQL, params)]
