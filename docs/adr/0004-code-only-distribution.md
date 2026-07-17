# ADR 0004: Code-only distribution

- **Status:** Accepted
- **Date:** 2026-07-17
- **Founder decision:** D4 (Data distribution), D11 (Code license)
- **Invariants:** §V16, §V17, §V19

## Context

Redistributing raw game-data snapshots or a prebuilt database would carry the
highest legal risk (imported content is governed by rights holders and sources
with no assumed dataset license) and is unnecessary to prove the product.

## Decision

Releases distribute **code, schema, migrations, tests, and parsers only** — no
raw snapshots and no prebuilt database. Local users build their own database via
`import`/`sync`; a private server builds its own internal, non-downloadable
database. **Apache-2.0 (`LICENSE`) covers project code only**; `NOTICE` records
that imported data and third-party game content are separately governed. No MCP
tool supports bulk dump, database download, or unbounded enumeration (§V19).
`data/builds/` and `*.sqlite` are git-ignored.

## Consequences

- Release artifacts contain no game content (§V16); auditable in CI/release
  checks (T49).
- Every imported record is provenance-stamped so it can be attributed and
  purged (§V17).
- Users need a snapshot and one build step before first use.
- Public access, monetization, or DB distribution requires a **new founder
  decision + legal review + written public data-distribution policy** — a new
  ADR, not a config flag.
