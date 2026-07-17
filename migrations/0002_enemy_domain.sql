-- 0002 enemy domain (SPEC §T13; PRD §12.4).
-- enemies, enemy_levels, enemy_aliases. Source identity = (server, game_id).

CREATE TABLE enemies (
    enemy_pk           INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_entity_id TEXT,
    server             TEXT NOT NULL,
    game_id            TEXT NOT NULL,
    display_name       TEXT,
    enemy_class        TEXT,
    is_boss            INTEGER NOT NULL DEFAULT 0 CHECK (is_boss IN (0, 1)),
    is_elite           INTEGER NOT NULL DEFAULT 0 CHECK (is_elite IN (0, 1)),
    attack_type        TEXT,
    motion_type        TEXT,
    provenance_id      INTEGER NOT NULL REFERENCES record_provenance (provenance_id),
    UNIQUE (server, game_id)
);

CREATE TABLE enemy_levels (
    enemy_level_pk       INTEGER PRIMARY KEY AUTOINCREMENT,
    enemy_pk             INTEGER NOT NULL REFERENCES enemies (enemy_pk),
    level_variant        INTEGER NOT NULL,
    hp                   INTEGER,
    atk                  INTEGER,
    def                  INTEGER,
    res                  INTEGER,
    attack_interval      REAL,
    attack_range         REAL,
    move_speed           REAL,
    weight               INTEGER,
    life_point_reduction INTEGER,
    block_behavior       TEXT,
    targeting_json       TEXT,
    immunities_json      TEXT,
    abilities_json       TEXT,
    UNIQUE (enemy_pk, level_variant)
);

CREATE TABLE enemy_aliases (
    enemy_pk         INTEGER NOT NULL REFERENCES enemies (enemy_pk),
    alias            TEXT NOT NULL,
    language         TEXT,
    normalized_alias TEXT,
    alias_type       TEXT
);

CREATE INDEX idx_enemy_aliases_enemy ON enemy_aliases (enemy_pk);
CREATE INDEX idx_enemy_aliases_norm ON enemy_aliases (normalized_alias);
