-- 0008 stage-scoped inline enemy variants (SPEC §T80; §V29/§V43/§V18).
-- A useDb:false wave-action ref is a LEVEL-INLINE enemy variant whose real stats
-- live in overwrittenData and differ from its base prefab (B37/§V43). Model each
-- as a STAGE-SCOPED variant row -- never a global enemy: the same inline id resolves
-- to a different prefab base across levels, and the same id is db-backed elsewhere
-- (B37) -- with a prefab_base FK + provenance + region (via stage_pk). Spawns and
-- occurrences of a variant carry variant_pk so get_stage + the threat analyzers read
-- the variant's stats OVER the base (COALESCE), resolving the §V43 limitation.
--
-- Stat columns mirror the §V29-verified enemy_levels stat set (attributes.<stat>
-- .m_value + lifePointReduce + motion); a NULL column means the variant did not
-- override that stat (m_defined:false) so the base value is inherited at read.

CREATE TABLE stage_enemy_variants (
    variant_pk           INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_pk             INTEGER NOT NULL REFERENCES stages (stage_pk),
    variant_id           TEXT NOT NULL,
    prefab_base_enemy_pk INTEGER NOT NULL REFERENCES enemies (enemy_pk),
    hp                   INTEGER,
    atk                  INTEGER,
    def                  INTEGER,
    res                  INTEGER,
    attack_interval      REAL,
    move_speed           REAL,
    weight               INTEGER,
    life_point_reduction INTEGER,
    motion_type          TEXT,
    source_fragment_json TEXT,
    provenance_id        INTEGER NOT NULL REFERENCES record_provenance (provenance_id),
    UNIQUE (stage_pk, variant_id)
);

CREATE INDEX idx_stage_enemy_variants_stage ON stage_enemy_variants (stage_pk);
CREATE INDEX idx_stage_enemy_variants_base ON stage_enemy_variants (prefab_base_enemy_pk);

ALTER TABLE stage_spawns ADD COLUMN variant_pk INTEGER REFERENCES stage_enemy_variants (variant_pk);

-- Recreate stage_enemies with variant_pk in the identity so two DISTINCT inline
-- variants of the SAME base prefab + level do not collapse into one occurrence
-- (each carries its own overridden stats). Safe to drop+recreate: migrations replay
-- against a fresh candidate before any import, so the table is empty here. This
-- reproduces 0003's columns + 0006's provenance_id + the new variant_pk. variant_pk
-- is NULL for a base-enemy occurrence; SQLite treats PK NULLs as distinct, and the
-- importer's per-key aggregation is the uniqueness guarantor (same contract as
-- 0006's importer-populated provenance_id).
DROP TABLE stage_enemies;
CREATE TABLE stage_enemies (
    stage_pk            INTEGER NOT NULL REFERENCES stages (stage_pk),
    enemy_pk            INTEGER NOT NULL REFERENCES enemies (enemy_pk),
    enemy_level_variant INTEGER NOT NULL,
    variant_pk          INTEGER REFERENCES stage_enemy_variants (variant_pk),
    total_count         INTEGER,
    first_spawn_time    REAL,
    last_spawn_time     REAL,
    route_count         INTEGER,
    provenance_id       INTEGER REFERENCES record_provenance (provenance_id),
    PRIMARY KEY (stage_pk, enemy_pk, enemy_level_variant, variant_pk)
);
