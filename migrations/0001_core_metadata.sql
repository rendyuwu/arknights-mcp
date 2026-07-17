-- 0001 core metadata tables (SPEC §T12; PRD §12.2).
-- schema_migrations, data_sources, source_snapshots, record_provenance,
-- source_policy_events. All foreign keys are declared and enforced (§12.1).

CREATE TABLE schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    checksum   TEXT NOT NULL
);

CREATE TABLE data_sources (
    source_id             TEXT PRIMARY KEY,
    display_name          TEXT NOT NULL,
    owner_name            TEXT NOT NULL,
    canonical_url         TEXT NOT NULL,
    source_type           TEXT NOT NULL,
    regions_json          TEXT NOT NULL,
    adapter_version       TEXT NOT NULL,
    license_identifier    TEXT,
    license_status        TEXT NOT NULL,
    permission_status     TEXT NOT NULL,
    private_hosting_status TEXT,
    redistribution_status TEXT NOT NULL,
    attribution_text      TEXT NOT NULL,
    contact_url           TEXT,
    policy_notes          TEXT,
    enabled               INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    last_reviewed_at      TEXT NOT NULL
);

CREATE TABLE source_snapshots (
    snapshot_id              TEXT PRIMARY KEY,
    source_id                TEXT NOT NULL REFERENCES data_sources (source_id),
    server                   TEXT NOT NULL,
    upstream_version         TEXT,
    commit_sha               TEXT,
    etag                     TEXT,
    fetched_at               TEXT,
    imported_at              TEXT NOT NULL,
    manifest_hash            TEXT NOT NULL,
    status                   TEXT NOT NULL,
    license_status_at_import TEXT,
    field_policy_version     TEXT NOT NULL
);

CREATE INDEX idx_source_snapshots_source ON source_snapshots (source_id);
CREATE INDEX idx_source_snapshots_server ON source_snapshots (server);

CREATE TABLE record_provenance (
    provenance_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id          TEXT NOT NULL REFERENCES source_snapshots (snapshot_id),
    source_path          TEXT NOT NULL,
    source_record_key    TEXT NOT NULL,
    record_hash          TEXT NOT NULL,
    transform_version    TEXT NOT NULL,
    field_policy_version TEXT NOT NULL
);

CREATE INDEX idx_record_provenance_snapshot ON record_provenance (snapshot_id);

CREATE TABLE source_policy_events (
    event_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id            TEXT NOT NULL REFERENCES data_sources (source_id),
    event_type           TEXT NOT NULL CHECK (
        event_type IN ('enable', 'disable', 'purge', 'permission_review', 'attribution_change')
    ),
    reason               TEXT,
    created_at           TEXT NOT NULL,
    actor_id             TEXT,
    result_manifest_hash TEXT
);

CREATE INDEX idx_source_policy_events_source ON source_policy_events (source_id);
