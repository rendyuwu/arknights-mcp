-- 0014 skin gallery domain (SPEC §T182; §V17/§V88; ADR 0015).
-- operator_skins backs the NAMED skin gallery: skin_table.json charSkins metadata
-- (skin id + display name + char id + portraitId + alt-form tmplId) sourced from the
-- SAME primary arknights_assets_gamedata snapshot as enemy/stage/operator (fetched
-- via SUPPLEMENTARY_FILES per §V41/B36) -- NOT a new source, adapter, or registry
-- entry. The mirror image URL stays QUERY-TIME DERIVED from portrait_id (§V63
-- derive-not-store): no URL and no bytes are ever persisted here.
--
-- Scope is METADATA-ONLY (§V18, extends §V16): a skin row stores only ids, a short
-- display name, and the outfit-group labels. There is deliberately NO column for
-- displaySkin prose (content / dialog / usage / description / drawerList /
-- modelName) -- the gallery is a naming FACT, never outfit flavor copy. A regression
-- test asserts the column set carries no prose/body/html/image column.
--
-- char_id is the raw source charId; for alt-form skins (Amiya family) charId names
-- the BASE operator while tmpl_id names the alternate playable form, so
-- tmpl_id != char_id is the alt-form discriminator (ADR 0015 -- char_patch_table is
-- NOT imported; tmplId alone carries the link). operator_pk SOFT-resolves to an
-- operators row when that operator is present in the SAME snapshot, else stays NULL
-- (operators are optional-zero per B36; an unresolvable skin never fails the build).
-- There is deliberately NO `resolved` flag column: `operator_pk IS NULL` already IS
-- the resolution state, and nothing reads a second encoding of it (§V94, B123 --
-- banner_featured_ops keeps its own `resolved` because get_banners emits it).
-- Token skins (charId not 'char_%') are filtered by the importer.
--
-- Every row carries a provenance_id FK (§V17). region is NOT NULL and ∈ {en,cn}
-- (§V5, enforced by the importer); en and cn skins are never silently mixed.
--
-- operator_skins stays OUT of CRITICAL_TABLES: skin_table is fetched tolerant-absent
-- (§V41/B36 -- a combat-only snapshot legitimately lacks it), so an empty skin
-- domain is a legitimate build (like banners/items/announcements). §V30's non-empty
-- guard is enforced by the importer (T182), not by CRITICAL_TABLES.
--
-- portrait_id is NOT NULL: it is the sole input to the derived mirror URL
-- (skin/<portrait_id>b.png); an operator-skin entry without one cannot be surfaced
-- and is skipped by the importer (fail-closed, no fabricated row). portrait_id is
-- deliberately NOT unique -- two skin ids may legally share one art asset.
-- Identity is (server, skin_id); a duplicate collides so the importer maps the
-- anomaly to a typed ImporterError (§V33), never an uncaught IntegrityError tearing
-- down the multi-region build.

CREATE TABLE operator_skins (
    skin_pk         INTEGER PRIMARY KEY AUTOINCREMENT,
    server          TEXT NOT NULL,
    skin_id         TEXT NOT NULL,
    char_id         TEXT NOT NULL,
    tmpl_id         TEXT,
    operator_pk     INTEGER REFERENCES operators (operator_pk),
    display_name    TEXT,
    skin_group_id   TEXT,
    skin_group_name TEXT,
    portrait_id     TEXT NOT NULL,
    is_buy_skin     INTEGER CHECK (is_buy_skin IN (0, 1)),
    region          TEXT NOT NULL,
    provenance_id   INTEGER NOT NULL REFERENCES record_provenance (provenance_id),
    UNIQUE (server, skin_id)
);

-- Serves the get_operator gallery read (OperatorRepository.skins):
-- `WHERE server = ? AND operator_pk = ?` -- the query filters BOTH leading columns
-- (§V94/B115: a WHERE skipping `server` would not use this index at all).
CREATE INDEX idx_operator_skins_operator ON operator_skins (server, operator_pk);

-- No char_id index: nothing reads skins by raw source char id (§V94/B122). The
-- importer resolves char_id -> operator_pk through the operators table, purge selects
-- by provenance_id, and the cross-region gate joins operator_pk; an index here would
-- only add write amplification. Add one WITH the query that needs it, never before.
