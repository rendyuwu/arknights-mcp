-- 0015 zone event title (SPEC §T205; §V110/B155).
-- `zones.display_name` holds what zone_table calls a zone: the SUB-ZONE SUBTITLE
-- ("The Coming of The Future"). The EVENT TITLE a client searches by ("Lone Trail")
-- is a different string from a different file -- activity_table.json's
-- basicInfo[<actId>].name, joined to the zone through its zoneToActivity map -- and
-- it appears nowhere in zone_table. Two names, so two columns: conflating them into
-- display_name would lose the subtitle and make an alias hit unattributable (§V90 --
-- both ride the wire, one key each).
--
-- event_name is NULLABLE and stays NULL for the 44 EN zones that have no activity row
-- at all (camp_zone_* annihilation, tower_* CLIMB_TOWER, rogue_* IS, guide_1) and for
-- any snapshot lacking activity_table (fetched tolerant-absent, §V41/B36). A NULL is
-- the honest "no title in source" (§V26/§V67 -- the tool layer omits the key), never a
-- fabricated one.
--
-- activity_game_id is the raw source actId the title came from: it is the join key the
-- title was resolved through, so a stale/renamed event stays traceable to its source
-- record without re-reading the snapshot. No index on either column -- nothing looks a
-- zone up BY event (search goes through entity_fts, and the zones join is by zone_pk),
-- and an index added before its query is write amplification (§V94/B122).
--
-- event_provenance_id is a SECOND provenance FK because the title is a second SOURCE
-- FILE (§V17): provenance_id points at the zone_table record, event_provenance_id at
-- the activity_table one, and one activity row is shared by that event's zones. Both
-- files belong to the same source + snapshot, so a purge (§V20, per source_id) removes
-- the zone row and both provenance rows together -- an event title can never outlive
-- the record it came from.
ALTER TABLE zones ADD COLUMN event_name TEXT;
ALTER TABLE zones ADD COLUMN activity_game_id TEXT;
ALTER TABLE zones ADD COLUMN event_provenance_id INTEGER REFERENCES record_provenance (provenance_id);
