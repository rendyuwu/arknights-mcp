-- 0009 drop-rate domain (SPEC §T88; §V17/§V53/§V54).
-- items + stage_drops back the v0.2 M8 penguin_statistics drop-rate cache. A drop
-- rate is a penguin-sourced FACT with its OWN provenance chain, distinct from the
-- arknights_assets game-data FACT (§V54) -- so every row carries a provenance_id FK
-- (snapshot_id + source_path/key + record_hash + transform_version, §V17) plus, per
-- §V53, the penguin snapshot_id, region, fetched_at, and expires_at needed to serve
-- a stale-aware, attributed drop fact (past expires_at -> data_stale, §V24/§V53).
--
-- Both tables stay OUT of CRITICAL_TABLES: penguin is optional/disabled-by-default,
-- so an empty drops domain is a legitimate build (unlike the combat/operator domains).
-- The importer (T89) resolves penguin stageId/itemId to the internal stage_pk/item_pk
-- FKs and skips a drop whose stage/item is absent (fail-closed, no fabricated row).

CREATE TABLE items (
    item_pk       INTEGER PRIMARY KEY AUTOINCREMENT,
    server        TEXT NOT NULL,
    game_id       TEXT NOT NULL,
    display_name  TEXT,
    rarity        TEXT,
    item_type     TEXT,
    provenance_id INTEGER NOT NULL REFERENCES record_provenance (provenance_id),
    UNIQUE (server, game_id)
);

-- No explicit items index: UNIQUE(server, game_id) already builds a covering index
-- that serves the get_stage_drops by-game-id read; a duplicate CREATE INDEX on the
-- same columns would only add write amplification.

-- One aggregated drop observation per (stage, item): quantity dropped over `times`
-- sampled runs with penguin's reported drop_rate. region is carried explicitly (§V5)
-- even though it is implied by the stage/item server, so a drop fact stands alone.
CREATE TABLE stage_drops (
    drop_pk       INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_pk      INTEGER NOT NULL REFERENCES stages (stage_pk),
    item_pk       INTEGER NOT NULL REFERENCES items (item_pk),
    region        TEXT NOT NULL,
    quantity      INTEGER,
    times         INTEGER,
    drop_rate     REAL,
    snapshot_id   TEXT NOT NULL REFERENCES source_snapshots (snapshot_id),
    fetched_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    provenance_id INTEGER NOT NULL REFERENCES record_provenance (provenance_id),
    UNIQUE (stage_pk, item_pk)
);

-- No explicit stage_pk index: UNIQUE(stage_pk, item_pk) already builds a composite
-- index whose leading column serves the get_stage_drops `WHERE stage_pk = ?` read.
-- Only the reverse item_pk lookup (not a leading column of any unique index) needs
-- its own index.
CREATE INDEX idx_stage_drops_item ON stage_drops (item_pk);
