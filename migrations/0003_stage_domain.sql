-- 0003 stage domain (SPEC §T14; PRD §12.5).
-- zones, stages, stage_maps, stage_tiles, stage_routes, stage_waves,
-- stage_spawns, and the derived stage_enemies summary. Depends on 0002
-- (stage_spawns.enemy_pk -> enemies.enemy_pk).

CREATE TABLE zones (
    zone_pk      INTEGER PRIMARY KEY AUTOINCREMENT,
    server       TEXT NOT NULL,
    game_id      TEXT NOT NULL,
    display_name TEXT,
    zone_type    TEXT,
    UNIQUE (server, game_id)
);

CREATE TABLE stages (
    stage_pk           INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_entity_id TEXT,
    server             TEXT NOT NULL,
    game_id            TEXT NOT NULL,
    stage_code         TEXT,
    display_name       TEXT,
    zone_pk            INTEGER REFERENCES zones (zone_pk),
    stage_type         TEXT,
    difficulty         TEXT,
    sanity_cost        INTEGER,
    recommended_level  INTEGER,
    max_life_points    INTEGER,
    level_source_path  TEXT,
    provenance_id      INTEGER NOT NULL REFERENCES record_provenance (provenance_id),
    UNIQUE (server, game_id)
);

CREATE INDEX idx_stages_code ON stages (server, stage_code);

CREATE TABLE stage_maps (
    stage_pk         INTEGER PRIMARY KEY REFERENCES stages (stage_pk),
    width            INTEGER,
    height           INTEGER,
    map_version      TEXT,
    environment_json TEXT
);

CREATE TABLE stage_tiles (
    stage_pk                INTEGER NOT NULL REFERENCES stages (stage_pk),
    x                       INTEGER NOT NULL,
    y                       INTEGER NOT NULL,
    tile_key                TEXT,
    height_type             TEXT,
    buildable_type          TEXT,
    passable                INTEGER,
    special_properties_json TEXT,
    PRIMARY KEY (stage_pk, x, y)
);

CREATE TABLE stage_routes (
    route_pk            INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_pk            INTEGER NOT NULL REFERENCES stages (stage_pk),
    route_index         INTEGER NOT NULL,
    start_position_json TEXT,
    end_position_json   TEXT,
    checkpoints_json    TEXT,
    UNIQUE (stage_pk, route_index)
);

CREATE TABLE stage_waves (
    wave_pk         INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_pk        INTEGER NOT NULL REFERENCES stages (stage_pk),
    wave_index      INTEGER NOT NULL,
    pre_delay       REAL,
    max_time_waiting REAL,
    UNIQUE (stage_pk, wave_index)
);

CREATE TABLE stage_spawns (
    spawn_pk            INTEGER PRIMARY KEY AUTOINCREMENT,
    wave_pk             INTEGER NOT NULL REFERENCES stage_waves (wave_pk),
    enemy_pk            INTEGER NOT NULL REFERENCES enemies (enemy_pk),
    enemy_level_variant INTEGER,
    route_pk            INTEGER REFERENCES stage_routes (route_pk),
    spawn_time          REAL,
    count               INTEGER,
    interval            REAL,
    spawn_group         TEXT,
    hidden_or_scripted  INTEGER,
    source_fragment_json TEXT
);

CREATE INDEX idx_stage_spawns_wave ON stage_spawns (wave_pk);
CREATE INDEX idx_stage_spawns_enemy ON stage_spawns (enemy_pk);

CREATE TABLE stage_enemies (
    stage_pk            INTEGER NOT NULL REFERENCES stages (stage_pk),
    enemy_pk            INTEGER NOT NULL REFERENCES enemies (enemy_pk),
    enemy_level_variant INTEGER NOT NULL,
    total_count         INTEGER,
    first_spawn_time    REAL,
    last_spawn_time     REAL,
    route_count         INTEGER,
    PRIMARY KEY (stage_pk, enemy_pk, enemy_level_variant)
);
