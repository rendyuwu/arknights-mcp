-- 0010 announcement metadata domain (SPEC §T94; §V16/§V17/§V56).
-- announcements backs the v0.2 M9 official-announcement adapter (D14). The scope is
-- METADATA-ONLY (§V56, extends §V16): a row stores only announce_id + title + date +
-- url + category + region. There is deliberately NO column for the article body,
-- html, prose, image, or image-url -- the full announcement BODY is never stored,
-- so the schema itself cannot hold one. A regression test asserts the column set is
-- a subset of these metadata fields, tripping a future body/html column.
--
-- Every row carries a provenance_id FK (snapshot_id + source_path/key + record_hash
-- + transform_version, §V17). region is NOT NULL and ∈ {en,cn} (§V5, enforced by the
-- importer); en and cn announcements are never silently mixed.
--
-- announcements stays OUT of CRITICAL_TABLES: the adapter is disabled by default
-- (D14/§V56), so an empty announcement domain is a legitimate build (like the
-- items/stage_drops drop domain, unlike the combat/operator domains).
--
-- title/date/url/category are nullable metadata (a real feed row may omit any). The
-- importer (T95) resolves and sanitizes each string leaf, caps its length, and skips
-- a row missing an announce_id (fail-closed, no fabricated row).

CREATE TABLE announcements (
    announcement_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    region          TEXT NOT NULL,
    announce_id     TEXT NOT NULL,
    title           TEXT,
    date            TEXT,
    url             TEXT,
    category        TEXT,
    provenance_id   INTEGER NOT NULL REFERENCES record_provenance (provenance_id),
    UNIQUE (region, announce_id)
);

-- No explicit region/announce_id index: UNIQUE(region, announce_id) already builds a
-- composite index whose leading column serves the per-region get_announcements read.
