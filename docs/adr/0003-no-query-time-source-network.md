# ADR 0003: No query-time source network access

- **Status:** Accepted
- **Date:** 2026-07-17
- **Founder decision:** D3 (Data acquisition), D7 (Update cadence)
- **Invariants:** §V1, §V2, §V24

## Context

Fetching upstream data while answering a user's question would create
uncontrolled network egress, non-determinism, latency, and legal/abuse exposure
(scraping on demand). It would also blur the boundary between the read path and
the acquisition path.

## Decision

**User-facing MCP tools read SQLite only and never access an upstream source.**
Source downloads happen exclusively in explicit CLI `sync`/`import` jobs, which
are the only components allowed to touch the allowlisted sources. When a core
entity is absent, tools return a typed `not_found` / `region_unavailable` /
`data_stale` result with a suggested administrative action (e.g., run `sync`) —
**never** a query-time download or scrape fallback.

## Consequences

- Deterministic, offline-capable read path (§V11 determinism, §V1).
- Clear separation: acquisition (CLI, may touch net) vs. serving (MCP,
  never).
- Freshness is an operational concern surfaced via `get_data_status` and typed
  `data_stale` results, not fixed by silent fetching.
- Reversal (any runtime fetch) would require a new ADR and a security review.
