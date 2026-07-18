-- 0007 entity search index (SPEC §T31).
-- One unified FTS5 index over the searchable entity surface: operator / enemy /
-- stage. Indexed columns are the §T31 set (game_id, name, aliases, stage_code,
-- tags); identity columns are UNINDEXED so a hit maps back to its typed row
-- (entity_type + server + entity_pk) without being tokenized. The index is
-- populated at build time by importers/search_index.py (an MCP process opens the
-- promoted DB read-only, §V2, so it never writes the index at query time).
--
-- unicode61 is the default tokenizer (case- and diacritic-folding); prefix
-- queries ("term"*) give the search service forgiving name/alias matching.
-- Standalone (not external-content): the candidate is immutable once promoted,
-- so the index needs no triggers to track base-table edits.

CREATE VIRTUAL TABLE entity_fts USING fts5(
    game_id,
    name,
    aliases,
    stage_code,
    tags,
    entity_type UNINDEXED,
    server UNINDEXED,
    entity_pk UNINDEXED,
    tokenize = 'unicode61'
);
