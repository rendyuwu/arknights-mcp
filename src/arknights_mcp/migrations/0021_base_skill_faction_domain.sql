-- 0021 base-skill, faction, and collab domain (ADR 0021).
-- base_skills backs the operators' base (RIIC) skills: building_data.json `buffs`
-- (buff id + name + facility roomType + in-game mechanic effect text) sourced from the
-- SAME primary arknights_assets_gamedata snapshot as enemy/stage/operator (fetched
-- via SUPPLEMENTARY_FILES) -- NOT a new source, adapter, or registry entry.
-- operator_base_skills links each operator to its two base-skill slots: `slot_index`
-- and `stage_index` are 1-based positions in `buffChar[]` and `buffData[]`, and each
-- stage carries its own unlock gate (elite phase + level). A later stage in a slot
-- replaces the earlier one once unlocked.
--
-- operator_factions is a child of operators with no provenance of its own (the
-- operator_aliases pattern): character_table mainPower/subPower faction ids with the
-- handbook_team_table display name joined at parse time, as subclass_name is (0019).
-- operators.collab is the handbook_info_table `isLimited` flag -- the ONLY field read
-- from that file; its lore stays out. NULL means unknown (the file is absent or has
-- no entry), 0 flagged not-limited, 1 collab.
--
-- Every base-skill row carries a provenance_id FK. The domain is optional: all three
-- files are fetched tolerant-absent (a combat-only snapshot legitimately lacks them),
-- so the tables stay OUT of CRITICAL_TABLES and an empty domain is a legitimate build.

CREATE TABLE base_skills (
    base_skill_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    server        TEXT NOT NULL,
    buff_id       TEXT NOT NULL,
    display_name  TEXT,
    room_type     TEXT NOT NULL,
    description   TEXT,
    provenance_id INTEGER NOT NULL REFERENCES record_provenance (provenance_id),
    UNIQUE (server, buff_id)
);

CREATE TABLE operator_base_skills (
    operator_pk   INTEGER NOT NULL REFERENCES operators (operator_pk),
    slot_index    INTEGER NOT NULL,
    stage_index   INTEGER NOT NULL,
    base_skill_pk INTEGER NOT NULL REFERENCES base_skills (base_skill_pk),
    unlock_phase  INTEGER NOT NULL,
    unlock_level  INTEGER NOT NULL,
    provenance_id INTEGER NOT NULL REFERENCES record_provenance (provenance_id),
    PRIMARY KEY (operator_pk, slot_index, stage_index)
);
-- no secondary index: every read filters on operator_pk, which leads the primary key

CREATE TABLE operator_factions (
    operator_pk  INTEGER NOT NULL REFERENCES operators (operator_pk),
    faction_id   TEXT NOT NULL,
    display_name TEXT,
    is_main      INTEGER NOT NULL CHECK (is_main IN (0, 1)),
    PRIMARY KEY (operator_pk, faction_id)
);
-- no secondary index: faction reads filter on operator_pk (the primary key) or scan a
-- few hundred rows

ALTER TABLE operators ADD COLUMN collab INTEGER CHECK (collab IN (0, 1));
