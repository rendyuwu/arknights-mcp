-- 0004 operator domain (SPEC §T19; PRD §12.3).
-- operators, operator_aliases, operator_phases, skills, operator_skills,
-- skill_levels, talents, talent_levels, modules, module_levels. Source identity
-- = (server, game_id). Core rows carry provenance_id -> record_provenance (§V17);
-- sub-tables link through their parent. gameplay_description columns are
-- nullable and policy-controlled (§V16: excluded by default unless field policy
-- permits). Depends on 0001 (record_provenance).

CREATE TABLE operators (
    operator_pk         INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_entity_id TEXT,
    server              TEXT NOT NULL,
    game_id             TEXT NOT NULL,
    display_name        TEXT,
    rarity              INTEGER,
    profession          TEXT,
    subclass_id         TEXT,
    position            TEXT,
    tag_json            TEXT,
    obtainable          INTEGER CHECK (obtainable IN (0, 1)),
    provenance_id       INTEGER NOT NULL REFERENCES record_provenance (provenance_id),
    UNIQUE (server, game_id)
);

CREATE TABLE operator_aliases (
    operator_pk      INTEGER NOT NULL REFERENCES operators (operator_pk),
    alias            TEXT NOT NULL,
    language         TEXT,
    normalized_alias TEXT,
    alias_type       TEXT
);

CREATE INDEX idx_operator_aliases_operator ON operator_aliases (operator_pk);
CREATE INDEX idx_operator_aliases_norm ON operator_aliases (normalized_alias);

CREATE TABLE operator_phases (
    operator_pk     INTEGER NOT NULL REFERENCES operators (operator_pk),
    phase           INTEGER NOT NULL,
    max_level       INTEGER,
    max_hp          INTEGER,
    atk             INTEGER,
    def             INTEGER,
    res             INTEGER,
    redeploy_time   INTEGER,
    cost            INTEGER,
    block_count     INTEGER,
    attack_interval REAL,
    range_id        TEXT,
    PRIMARY KEY (operator_pk, phase)
);

CREATE TABLE skills (
    skill_pk      INTEGER PRIMARY KEY AUTOINCREMENT,
    server        TEXT NOT NULL,
    game_id       TEXT NOT NULL,
    display_name  TEXT,
    skill_type    TEXT,
    sp_type       TEXT,
    duration_type TEXT,
    provenance_id INTEGER NOT NULL REFERENCES record_provenance (provenance_id),
    UNIQUE (server, game_id)
);

CREATE TABLE operator_skills (
    operator_pk  INTEGER NOT NULL REFERENCES operators (operator_pk),
    skill_pk     INTEGER NOT NULL REFERENCES skills (skill_pk),
    slot_index   INTEGER NOT NULL,
    unlock_phase INTEGER,
    unlock_level INTEGER,
    PRIMARY KEY (operator_pk, slot_index)
);

CREATE INDEX idx_operator_skills_skill ON operator_skills (skill_pk);

CREATE TABLE skill_levels (
    skill_pk             INTEGER NOT NULL REFERENCES skills (skill_pk),
    level                INTEGER NOT NULL,
    sp_cost              INTEGER,
    initial_sp           INTEGER,
    duration             REAL,
    range_id             TEXT,
    blackboard_json      TEXT,
    gameplay_description TEXT,
    PRIMARY KEY (skill_pk, level)
);

CREATE TABLE talents (
    talent_pk    INTEGER PRIMARY KEY AUTOINCREMENT,
    operator_pk  INTEGER NOT NULL REFERENCES operators (operator_pk),
    talent_index INTEGER NOT NULL,
    display_name TEXT,
    UNIQUE (operator_pk, talent_index)
);

CREATE TABLE talent_levels (
    talent_pk            INTEGER NOT NULL REFERENCES talents (talent_pk),
    variant_index        INTEGER NOT NULL,
    unlock_phase         INTEGER,
    unlock_level         INTEGER,
    potential_rank       INTEGER,
    condition_json       TEXT,
    blackboard_json      TEXT,
    gameplay_description TEXT,
    PRIMARY KEY (talent_pk, variant_index)
);

CREATE TABLE modules (
    module_pk           INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_entity_id TEXT,
    server              TEXT NOT NULL,
    game_id             TEXT NOT NULL,
    operator_pk         INTEGER NOT NULL REFERENCES operators (operator_pk),
    module_type         TEXT,
    display_name        TEXT,
    unlock_phase        INTEGER,
    unlock_level        INTEGER,
    provenance_id       INTEGER NOT NULL REFERENCES record_provenance (provenance_id),
    UNIQUE (server, game_id)
);

CREATE INDEX idx_modules_operator ON modules (operator_pk);

CREATE TABLE module_levels (
    module_pk            INTEGER NOT NULL REFERENCES modules (module_pk),
    level                INTEGER NOT NULL,
    stat_bonus_json      TEXT,
    trait_changes_json   TEXT,
    talent_changes_json  TEXT,
    cost_json            TEXT,
    gameplay_description TEXT,
    PRIMARY KEY (module_pk, level)
);
