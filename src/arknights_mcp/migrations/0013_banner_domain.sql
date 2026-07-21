-- 0013 banner archive domain (SPEC §T112; §V17/§V62).
-- banners + banner_featured_ops back the v0.2 M11 banner ARCHIVE: a historical
-- gacha-schedule FACT sourced from the SAME primary arknights_assets_gamedata
-- snapshot as enemy/stage/operator (gacha_table.json gachaPoolClient, fetched via
-- SUPPLEMENTARY_FILES per §V41/B36) -- NOT a new source, adapter, or registry entry.
--
-- Scope is METADATA-ONLY (§V62, extends §V16/§V56): a banner row stores only the
-- pool's game id, a short display name, the open/end schedule (unix epoch normalized
-- to ISO by the importer), the rule-type enum, region, and provenance. There is
-- deliberately NO column for gachaPoolSummary / gachaPoolDetail / dynMeta prose /
-- html / image -- the archive is a schedule FACT, never gacha promotional copy. A
-- regression test asserts the column set carries no prose/body/html/image column.
--
-- Archive =/= PLANNING: rate/pity/spark constants are VERIFIED absent from
-- gacha_table (§C) so no pull-probability column exists here either.
--
-- Every row carries a provenance_id FK (snapshot_id + source_path/key + record_hash
-- + transform_version, §V17). region is NOT NULL and ∈ {en,cn} (§V5, enforced by the
-- importer); en and cn banners are never silently mixed.
--
-- banners stays OUT of CRITICAL_TABLES: gacha_table is fetched tolerant-absent
-- (§V41/B36 -- a combat-only snapshot legitimately lacks it), so an empty banner
-- domain is a legitimate build (like the items/stage_drops and announcements
-- domains, unlike the combat/operator domains). §V30's non-empty guard is enforced
-- by the importer (T113), not by CRITICAL_TABLES.
--
-- display_name / open_time / end_time / rule_type are nullable metadata: a raw pool
-- entry may omit any, and the importer (T113) resolves + sanitizes + caps each leaf.
-- Identity is (server, game_id) = (server, gachaPoolId); a duplicate collides so the
-- importer can map the anomaly to a typed ImporterError (§V33), never an uncaught
-- IntegrityError tearing down the multi-region build.

CREATE TABLE banners (
    banner_pk     INTEGER PRIMARY KEY AUTOINCREMENT,
    server        TEXT NOT NULL,
    game_id       TEXT NOT NULL,
    display_name  TEXT,
    open_time     TEXT,
    end_time      TEXT,
    rule_type     TEXT,
    region        TEXT NOT NULL,
    provenance_id INTEGER NOT NULL REFERENCES record_provenance (provenance_id),
    UNIQUE (server, game_id)
);

-- The (server, open_time) index serves the get_banners read (T114): per-region
-- listing ordered/filtered by the schedule (optional since/until date filter).
CREATE INDEX idx_banners_server_open ON banners (server, open_time);

-- Typed featured operators (§V62): a LIMITED banner names one featured limited op
-- (limitParam.limitedCharId); a CLASSIC-family banner names an array
-- (dynMeta.attainRare6CharList); NORMAL/SINGLE/DOUBLE/LINKAGE carry NONE (no row).
-- char_id is the raw source id; operator_pk SOFT-resolves to an operators row when
-- that operator is present in the SAME snapshot, else stays NULL with resolved = 0
-- (operators are optional-zero per B36 ∴ a combat-only snapshot yields raw char_ids;
-- an unresolvable featured-op never fails the build -- the archive is a standalone
-- FACT, §V3/§V62 preserved).
--
-- UNIQUE(banner_pk, char_id): a banner lists each featured op once; a duplicate
-- char_id collides so the importer maps it to a typed ImporterError (§V33), and the
-- leading column (banner_pk) serves the get_banners forward join -- no separate
-- banner_pk index needed. purge cascades these child rows before banners (§V32, T113).
CREATE TABLE banner_featured_ops (
    banner_pk   INTEGER NOT NULL REFERENCES banners (banner_pk),
    operator_pk INTEGER REFERENCES operators (operator_pk),
    char_id     TEXT NOT NULL,
    resolved    INTEGER CHECK (resolved IN (0, 1)),
    UNIQUE (banner_pk, char_id)
);
