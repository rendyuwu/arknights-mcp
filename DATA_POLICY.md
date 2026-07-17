# Data Policy

This policy governs what data this project ingests, how it is transformed and
attributed, and what is never distributed. It derives from the founder-approved
decisions and the legal/distribution guardrails in the PRD (Section 10) and the
invariants in [`SPEC.md`](SPEC.md).

## Distribution guardrails (PRD Section 10.9)

1. Releases distribute **code, schema, migrations, tests, and parsers** — not
   raw snapshots and not a prebuilt database.
2. Local users create their database via the `sync` / `import` commands; a
   private server builds its own internal database.
3. **Apache-2.0 applies to project code only** and does not relicense imported
   data (see [`NOTICE`](NOTICE)).
4. The importer uses an **explicit gameplay-field allowlist** and excludes
   unused prose.
5. Full raw source files are **never** exposed through MCP tools or resources.
6. Every imported record stores its **source snapshot, source path/key,
   transform version, and content hash** (SPEC §V17).
7. The README includes an **unofficial-project disclaimer and trademark
   notice**.
8. **No game credentials** are requested, stored, or transmitted (SPEC §V15).
9. v0.1 remains **private and non-commercial**.
10. No tool supports **bulk dump, arbitrary SQL, unbounded pagination, or
    database download** (SPEC §V19).
11. Public access, monetization, or database distribution requires a **new
    founder decision, source-policy review, qualified legal review, and a
    written public data-distribution policy** — it cannot be enabled by a
    configuration flag.

## Field allowlist (SPEC §V18)

- The importer parses **only explicitly allowlisted gameplay fields**. The
  authoritative list lives in `src/arknights_mcp/importers/field_policy.py` and
  is versioned (`field_policy_version`).
- Imported strings are treated as **untrusted data**: they are never
  concatenated into server instructions or tool descriptions, control
  characters are stripped, and lengths are capped.
- The allowlist targets typed, structured gameplay fields (IDs, numeric stats,
  enumerations, structured map/wave/spawn data, canonical names, and aliases).
  Optional long-form gameplay descriptions are excluded by default and only
  retained where the policy explicitly permits.

## Excluded content (PRD Section 10.6)

The following are **never** ingested into core tables or distributed:

- PRTS or other wiki article bodies;
- community strategy guides and tier lists;
- Fandom/wiki prose;
- videos, screenshots, and map images;
- Reddit or other social-media commentary;
- story scripts, operator records, voice lines, art, animation, and audio;
- full official promotional articles or full announcement bodies.

(An AI host may independently browse public sources at a user's explicit
request; that is outside this MCP's data pipeline and is never cached into core
tables.)

## Provenance (SPEC §V17)

Every imported record is stamped with `snapshot_id`, `source_path` /
`source_record_key`, `transform_version`, and `record_hash`, linked through the
`record_provenance` table. Snapshots additionally record the source-policy /
field-policy version at import time. `get_data_status` reports the active
snapshot and commit/version per region; `get_data_sources` reports the
public-safe registry.

## Language policy (D6)

Original Simplified Chinese and available English/canonical aliases are
preserved. Tool keys and schemas are English. Bulk machine-translated source
descriptions are not stored.

## Region separation (SPEC §V5)

`en` and `cn` data are never silently mixed. Every region-sensitive entity and
factual response identifies its region and source snapshot.
