-- 0016 per-level skill facts.
-- `skill_table` scopes four fields PER LEVEL -- each level entry carries its own
-- `name`, `skillType`, `durationType` and `spData.spType` -- but the importer read
-- them off level 1 and stored one scalar per SKILL. For 1597 of 1598 EN skills the
-- levels agree and the scalar is true; for the rest it was a discarded fact. The
-- worst case is the studied skill: `sktok_mjcsdw` stored sp_type = "8",
-- the bare code the source ships no name for, while its OWN level 2 sends the named
-- INCREASE_WITH_TIME (and skill_type PASSIVE while level 2 is AUTO).
--
-- So the four columns move to where the source scopes them. `skills` keeps its four
-- columns and their meaning narrows to "the value every level shares", NULL when the
-- levels disagree (a uniform value is hoisted to the parent, a varying
-- one lives per level and the parent key is omitted on the wire, never a
-- representative pick). That mirrors what `skill_levels.gameplay_description` and the
-- module change bundles already do, so a client reads one shape for all of
-- them: value on the parent = same at every level.
--
-- All four are NULLABLE, and a per-level column is NULL in the common uniform case:
-- the value is on the skill row and repeating it 7-10 times per skill would be pure
-- write amplification for the ~99.9% of skills that never vary. NULL therefore reads
-- as "see the skill row", exactly as `gameplay_description` does when hoisted.
--
-- No index: nothing looks a skill up BY one of these values (the enum domains are
-- emitted, never queried), and an index added before its query is write amplification.
-- No new provenance FK either -- these are more fields of the same
-- `skill_table` record the parent skill's provenance_id already points at,
-- unlike 0015's event title, which came from a second file.
ALTER TABLE skill_levels ADD COLUMN display_name TEXT;
ALTER TABLE skill_levels ADD COLUMN skill_type TEXT;
ALTER TABLE skill_levels ADD COLUMN sp_type TEXT;
ALTER TABLE skill_levels ADD COLUMN duration_type TEXT;
