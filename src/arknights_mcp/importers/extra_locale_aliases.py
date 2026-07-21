"""Extra-locale alias importer: jp/kr NAME -> locale-tagged aliases (§T99).

Consumes what the CLI-only
:class:`~arknights_mcp.sources.extra_locale_aliases.ExtraLocaleAliasAdapter` returns
(never a query-time fetch, §V1) and attaches each jp/kr canonical NAME as a
locale-tagged alias on the *existing* en/cn entity that shares its ``game_id``:

* the field allowlist keeps only the canonical ``name`` from each source entry
  (``LOCALE_NAME_ALLOWLIST``), so a machine-translated description / prose is never
  stored (§V57 NAME-only, extends D6/§V18);
* the alias is stamped with its ``locale`` tag (``ja``/``ko``) -- NOT a fact region:
  the entity still returns its OWN en/cn region facts, and an alias match never
  widens region availability (§V57);
* a name attaches to *every* en/cn row of a game_id (a game_id present in both
  regions gets the alias on both), so an alias search resolves the entity in either
  region it was imported for (§V57 alias ≠ region).

Pure parsing (:func:`parse_character_names` / :func:`parse_enemy_handbook_names`) is
separated from the DB write so it is unit-testable without a database. The two
entity domains share one insert helper (:func:`_insert_locale_aliases`) parametrized
by target table -- no operator/enemy copy-paste (§V37). A non-empty source that
matches zero existing entities fails closed (§V30): a game_id-scheme mismatch would
otherwise import silently nothing. A genuinely empty source imports zero without
error (the extra-locale domain is legitimately optional).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.field_policy import (
    EXTRA_LOCALE_FOR_REGION,
    apply_allowlist,
)
from arknights_mcp.util.coerce import as_str

_LOG = logging.getLogger(__name__)

#: NAME-only allowlist for an extra-locale entry (§V57). Only the canonical display
#: NAME is read from a ``character_table`` / ``enemy_handbook_table`` entry; every
#: other field (prose ``description``, appellation, stats) is dropped before the
#: importer sees it, so a machine-translated description is never stored (D6/§V18).
LOCALE_NAME_ALLOWLIST: frozenset[str] = frozenset({"name"})


class ExtraLocaleFetcher(Protocol):
    """The read surface the importer needs from the extra-locale adapter (§V37).

    Matches :meth:`ExtraLocaleAliasAdapter.fetch`; typed as a Protocol so the importer
    is unit-testable with an in-memory fake and never depends on the network class.
    """

    def fetch(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class LocaleAliasImportResult:
    """Per-region outcome. ``candidate_names`` counts source entries that carried a
    NAME (before the game_id match); ``*_aliases_inserted`` count rows actually
    attached to an existing en/cn entity."""

    region: str
    locale: str
    operator_aliases_inserted: int
    enemy_aliases_inserted: int
    candidate_names: int


@dataclass(frozen=True)
class _AliasTarget:
    """One entity domain's alias-insert plumbing (literal SQL, no f-string build).

    Both the operator and enemy domains route through :func:`_insert_locale_aliases`
    with one of these targets, so the two never diverge (§V37). The SQL is fixed --
    only the bound values are caller-derived, so injection is impossible (§V2).
    """

    select_pk_sql: str
    insert_sql: str


_OPERATOR_TARGET = _AliasTarget(
    select_pk_sql="SELECT operator_pk FROM operators WHERE game_id = ?",
    insert_sql=(
        "INSERT INTO operator_aliases "
        "(operator_pk, alias, language, normalized_alias, alias_type, locale) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    ),
)

_ENEMY_TARGET = _AliasTarget(
    select_pk_sql="SELECT enemy_pk FROM enemies WHERE game_id = ?",
    insert_sql=(
        "INSERT INTO enemy_aliases "
        "(enemy_pk, alias, language, normalized_alias, alias_type, locale) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    ),
)


def _names_from_id_keyed(raw: Any) -> dict[str, str]:
    """Extract ``{game_id: canonical_name}`` from an id-keyed source table (§V57).

    Each entry is passed through :data:`LOCALE_NAME_ALLOWLIST` so only the sanitized
    ``name`` survives; an entry with no readable name is skipped (no fabricated
    alias). Non-string keys / non-dict entries are ignored.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for game_id, entry in raw.items():
        if not isinstance(game_id, str) or not isinstance(entry, dict):
            continue
        kept = apply_allowlist(entry, LOCALE_NAME_ALLOWLIST).kept
        name = as_str(kept.get("name"))
        # ``if name`` (not ``is not None``): a name that sanitized down to "" (all
        # control/whitespace) must not become an empty alias row -- it would be junk
        # in FTS and, worse, would count toward the §V30 "matched something" tally
        # and mask a real scheme mismatch. Matches the operator self-alias convention
        # (_operator_aliases uses ``if name:``).
        if name:
            out[game_id] = name
    return out


def parse_character_names(character_raw: Any) -> dict[str, str]:
    """Parse ``{operator game_id: locale name}`` from a locale ``character_table``."""
    return _names_from_id_keyed(character_raw)


def parse_enemy_handbook_names(enemy_handbook_raw: Any) -> dict[str, str]:
    """Parse ``{enemy game_id: locale name}`` from a locale ``enemy_handbook_table``.

    The real handbook wraps its id-keyed entries under a top-level ``enemyData`` key
    (matching the primary enemy importer, §V29); a bare id-keyed dict is also
    accepted so a pre-unwrapped fixture parses.
    """
    data = enemy_handbook_raw
    if isinstance(data, dict) and "enemyData" in data:
        data = data["enemyData"]
    return _names_from_id_keyed(data)


def _insert_locale_aliases(
    conn: sqlite3.Connection,
    names: dict[str, str],
    target: _AliasTarget,
    *,
    locale: str,
) -> tuple[int, int]:
    """Attach each name as a locale alias on every en/cn row of its game_id (§V57).

    A game_id present in both regions gets the alias on both rows (so the alias
    resolves the entity in either region it was imported for); a game_id absent from
    the entity table contributes no alias (nothing to attach to). ``normalized_alias``
    is the casefolded name (matching the operator alias importer, §V37); ``alias_type``
    marks it a locale name and ``locale`` carries the language tag.

    Returns ``(inserted, matched)``: ``inserted`` counts alias rows written; ``matched``
    counts source names whose game_id resolved to ≥1 existing entity. The two differ
    when a game_id spans both regions (1 match -> 2 inserts) or is absent (0/0). The
    §V30 guard keys on ``matched`` -- NOT ``inserted`` -- so it stays correct under a
    future ``INSERT OR IGNORE`` idempotency pass (a re-run matches the game_ids even
    when the row insert is suppressed, T109) (B51).
    """
    inserted = 0
    matched = 0
    for game_id, name in names.items():
        pk_rows = conn.execute(target.select_pk_sql, (game_id,)).fetchall()
        if pk_rows:
            matched += 1
        for (entity_pk,) in pk_rows:
            conn.execute(
                target.insert_sql,
                (entity_pk, name, None, name.casefold(), "locale_name", locale),
            )
            inserted += 1
    return inserted, matched


def import_locale_aliases(
    conn: sqlite3.Connection,
    adapter: ExtraLocaleFetcher,
    *,
    region: str,
) -> LocaleAliasImportResult:
    """Fetch + import one extra locale's NAME aliases onto existing entities (§T99).

    ``region`` must be a known extra locale (``jp``/``kr``, §V57); its stored locale
    tag comes from the shared :data:`EXTRA_LOCALE_FOR_REGION` map (single §V37 home).
    Only the NAME allowlist is stored (§V57/§V18). A non-empty source that matches no
    existing entity fails closed (§V30) -- a game_id-scheme mismatch would otherwise
    build silently empty; a genuinely empty source imports zero without error.
    """
    locale = EXTRA_LOCALE_FOR_REGION.get(region)
    if locale is None:
        allowed = "|".join(sorted(EXTRA_LOCALE_FOR_REGION))
        raise ImporterError(f"extra-locale region must be {allowed}, got {region!r} (§V57)")

    payload = adapter.fetch()
    op_names = parse_character_names(payload.get("character_table"))
    enemy_names = parse_enemy_handbook_names(payload.get("enemy_handbook"))
    candidate_names = len(op_names) + len(enemy_names)

    op_inserted, op_matched = _insert_locale_aliases(
        conn, op_names, _OPERATOR_TARGET, locale=locale
    )
    enemy_inserted, enemy_matched = _insert_locale_aliases(
        conn, enemy_names, _ENEMY_TARGET, locale=locale
    )

    # §V30 PER-DOMAIN (B51): the guard keys on MATCHED game_ids per domain, NOT the
    # combined insert count. A source feeds two domains (operator + enemy, §V57); a
    # sibling's success must not mask a per-domain game_id-scheme mismatch. If a domain
    # carried candidate names yet none matched an existing en/cn entity by game_id, that
    # domain's aliases would silently attach nothing while the other domain succeeds ->
    # a promoted build whose extra-locale search is half-dead. Fail closed so the
    # candidate is discarded and the active DB stays untouched (§V3). Keying on MATCHED
    # (not inserted) stays correct under T109 ``INSERT OR IGNORE`` idempotency: a re-run
    # still matches the game_ids even when the row insert is suppressed. A genuinely
    # empty domain (no candidate names) imports zero without error -- extra locale is
    # optional.
    for domain, names, matched in (
        ("operator", op_names, op_matched),
        ("enemy", enemy_names, enemy_matched),
    ):
        if names and matched == 0:
            raise ImporterError(
                f"{region}: extra-locale {domain} source carried {len(names)} name(s) "
                f"but none matched an existing en/cn {domain} by game_id; refusing a "
                f"silent empty extra-locale alias build for the {domain} domain (§V30)"
            )
    if candidate_names:
        _LOG.info(
            "extra-locale %s (%s): %d operator + %d enemy alias(es) from %d candidate name(s)",
            region,
            locale,
            op_inserted,
            enemy_inserted,
            candidate_names,
        )

    return LocaleAliasImportResult(
        region=region,
        locale=locale,
        operator_aliases_inserted=op_inserted,
        enemy_aliases_inserted=enemy_inserted,
        candidate_names=candidate_names,
    )
