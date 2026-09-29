-- 0011 alias locale tag.
-- Adds a `locale` column to the two near-identical alias tables (operator_aliases,
-- enemy_aliases -- kept symmetric) so each stored alias carries the
-- language-locale of its string. This unblocks the v0.2 extra-locale alias work:
-- jp/kr canonical NAMES import as locale-tagged rows and the FTS index rebuilds
-- with the `search_entities` `locale` param, which filters on this tag.
--
-- The locale tag is NOT the entity's fact region: a cn|en entity may carry a
-- jp/kr NAME alias, and an alias match still returns the entity's OWN region facts.
-- The backfill below tags each existing en/cn alias with the language its canonical
-- string is in -- en region -> locale `en`, cn region -> locale `zh` -- mirroring
-- REGION_TO_NAME_LOCALE in importers/field_policy.py (the single home for that
-- coupling, also used for penguin item names).
--
-- `locale` is a distinct column, NOT the pre-existing (always-NULL, unwired)
-- `language` column: `language` was reserved for a free-text language label and is
-- left untouched here; `locale` is the structured region/locale tag.
--
-- The ADD COLUMN is nullable so the additive ALTER is legal in SQLite and any
-- pre-existing row is not rejected. On a normal fresh build this migration runs
-- against an EMPTY candidate (migrations apply before the importers populate rows),
-- so the UPDATE below matches nothing -- the real fresh-build locale stamp is done by
-- the importer at insert time (operators._insert_aliases). The backfill exists for a
-- populated-DB / re-run path, so a DB carrying pre-0011 aliases still gets tagged.

ALTER TABLE operator_aliases ADD COLUMN locale TEXT;
ALTER TABLE enemy_aliases    ADD COLUMN locale TEXT;

CREATE INDEX idx_operator_aliases_locale ON operator_aliases (locale);
CREATE INDEX idx_enemy_aliases_locale    ON enemy_aliases (locale);

UPDATE operator_aliases
SET locale = (
    SELECT CASE o.server WHEN 'cn' THEN 'zh' ELSE o.server END
    FROM operators o
    WHERE o.operator_pk = operator_aliases.operator_pk
)
WHERE locale IS NULL;

UPDATE enemy_aliases
SET locale = (
    SELECT CASE e.server WHEN 'cn' THEN 'zh' ELSE e.server END
    FROM enemies e
    WHERE e.enemy_pk = enemy_aliases.enemy_pk
)
WHERE locale IS NULL;
