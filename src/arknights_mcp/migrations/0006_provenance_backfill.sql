-- 0006 per-record provenance for level-derived + zone rows (SPEC §V17).
-- Fixes the gap where rows derived from a stage's level file (a distinct
-- source_path) and zone rows carried no per-record provenance/tamper hash.
-- Each column is a nullable FK -> record_provenance; the importer always
-- populates it (one provenance row per level file / zone record). Nullable so
-- the additive ALTER is legal in SQLite and pre-existing rows are not rejected;
-- the importer is the guarantor that new rows are stamped.

ALTER TABLE zones         ADD COLUMN provenance_id INTEGER REFERENCES record_provenance (provenance_id);
ALTER TABLE stage_maps    ADD COLUMN provenance_id INTEGER REFERENCES record_provenance (provenance_id);
ALTER TABLE stage_tiles   ADD COLUMN provenance_id INTEGER REFERENCES record_provenance (provenance_id);
ALTER TABLE stage_routes  ADD COLUMN provenance_id INTEGER REFERENCES record_provenance (provenance_id);
ALTER TABLE stage_waves   ADD COLUMN provenance_id INTEGER REFERENCES record_provenance (provenance_id);
ALTER TABLE stage_spawns  ADD COLUMN provenance_id INTEGER REFERENCES record_provenance (provenance_id);
ALTER TABLE stage_enemies ADD COLUMN provenance_id INTEGER REFERENCES record_provenance (provenance_id);
