-- 0012 alias uniqueness for idempotent locale-alias re-import (SPEC §T109; §V57).
-- The extra-locale ride-along (T109) attaches jp/kr NAME aliases onto existing en/cn
-- entities every `sync`. A re-run / backfill would double-insert the SAME
-- (entity, alias, locale) row, which would then surface twice in the FTS
-- `GROUP_CONCAT` (a duplicated search token, §V37/B22). To make the import
-- idempotent, `_insert_locale_aliases` uses `INSERT OR IGNORE`; that conflict clause
-- needs a UNIQUE index to fire against.
--
-- SQLite has no `ALTER TABLE ... ADD CONSTRAINT`, so the uniqueness is added as a
-- UNIQUE INDEX rather than a table-level constraint -- functionally identical for the
-- OR IGNORE conflict resolution. The two alias tables stay symmetric (§V37).
--
-- Uniqueness key = (entity_pk, alias, locale), NOT (entity_pk, alias): the same
-- string can legitimately be BOTH the entity's own-region canonical name (locale
-- en/zh, stamped by operators._insert_aliases at insert time) AND an extra-locale
-- alias if a jp/kr name happens to match -- distinct locale tags, distinct rows.
--
-- Safe against the existing operator self-alias insert (operators._insert_aliases,
-- plain INSERT): its two rows per operator are `name` (alias_type=name) and
-- `appellation` (alias_type=appellation, only when appellation != name), both stamped
-- the same region locale -- distinct `alias` values ∴ never collide on this index.
-- The enemy importer inserts no self-alias (0 rows). On a normal fresh build this
-- migration runs against an EMPTY candidate (migrations apply before importers
-- populate rows), so no pre-existing duplicate can violate the new index.

CREATE UNIQUE INDEX idx_operator_aliases_unique
    ON operator_aliases (operator_pk, alias, locale);
CREATE UNIQUE INDEX idx_enemy_aliases_unique
    ON enemy_aliases (enemy_pk, alias, locale);
