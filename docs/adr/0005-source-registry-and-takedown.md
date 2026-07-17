# ADR 0005: Source registry, attribution, and takedown/purge

- **Status:** Accepted
- **Date:** 2026-07-17
- **Founder decision:** D13 (Hosted data permission posture), D14 (Official
  announcement ingestion)
- **Invariants:** §V20, §V27, §V28

## Context

The project imports third-party game data under a cautious posture: no dataset
license is assumed, and a public repo / attribution offer / takedown offer is
**not** treated as permission. Rights holders and source maintainers must be
able to see exactly what is used and to have it removed.

## Decision

Maintain a **machine-readable source registry** (`config/data_sources.toml`,
mirrored by `DATA_SOURCES.md` and the `get_data_sources` tool /
`arknights://sources` resource) that is complete for every enabled source:
`source_id`, owner, canonical URL, purpose/domains, regions,
license/permission status, private-hosting status, redistribution status,
attribution, contact, enabled state, last review, and snapshot commit. The
public-safe view excludes secrets, local paths, OAuth config, and takedown
correspondence (§V27).

Provide a **kill switch and purge/rebuild** flow (CLI-only, §V28):
`disable` stops new sync but keeps data; `purge <id> --rebuild` removes only
rows attributable to that source and rebuilds, with the current DB active until
the rebuilt candidate validates (§V20). Every action writes a
`source_policy_events` row. See `TAKEDOWN_POLICY.md`.

Official-announcement ingestion is **metadata-only at most** and disabled until
the core importer is stable and the source policy is reviewed (D14).

## Consequences

- Transparent, auditable source posture; fast, reversible removal.
- Admin surface stays off the MCP interface (§V28).
- Requires discipline: registry and `DATA_SOURCES.md` must be kept in sync
  (guarded by tests).
