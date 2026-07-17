-- 0005 analysis domain (SPEC §T19; PRD §12.6).
-- analysis_rules (rule registry) + analysis_findings (optional cache).
-- analysis_findings carries the §V6 observation fields: rule_id + evidence_json
-- + confidence + analyzer_version (limitations live inside finding_json, per the
-- PRD column list). entity_pk is polymorphic (stage_pk | operator_pk | ...)
-- keyed by entity_type, so it is not a single foreign key by design (§12.6).
-- Caching is optional in v0.1 (findings computed at query time); these tables
-- only need to exist. Depends on nothing beyond 0001.

CREATE TABLE analysis_rules (
    rule_id          TEXT PRIMARY KEY,
    rule_version     TEXT NOT NULL,
    domain           TEXT NOT NULL,
    name             TEXT NOT NULL,
    description      TEXT,
    severity_default TEXT,
    enabled          INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);

CREATE TABLE analysis_findings (
    finding_pk       INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type      TEXT NOT NULL,
    entity_pk        INTEGER NOT NULL,
    rule_id          TEXT NOT NULL REFERENCES analysis_rules (rule_id),
    severity         TEXT,
    confidence       REAL,
    finding_json     TEXT,
    evidence_json    TEXT,
    analyzer_version TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE INDEX idx_analysis_findings_entity ON analysis_findings (entity_type, entity_pk);
CREATE INDEX idx_analysis_findings_rule ON analysis_findings (rule_id);
