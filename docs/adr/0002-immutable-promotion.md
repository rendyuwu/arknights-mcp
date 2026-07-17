# ADR 0002: Immutable SQLite builds with atomic, validated promotion

- **Status:** Accepted
- **Date:** 2026-07-17
- **Founder decision:** D3 (Data acquisition), D7 (Update cadence)
- **Invariants:** §V3, §V4, §V20

## Context

Imports must never leave the running server with a corrupt, partial, or
schema-incompatible database. Updating in place risks readers observing a
half-written state and makes rollback hard.

## Decision

Builds are **immutable and versioned**. Each `sync`/`import` writes a new
candidate file `data/builds/<ts>-en-cn.sqlite` and **never mutates the active
database in place**. A candidate is promoted **only after** validation passes:

- `PRAGMA integrity_check`
- `PRAGMA foreign_key_check`
- critical-table checks, row-count sanity, and golden tests.

Promotion is **atomic** via `data/current.json` (immutable filename, DB hash,
schema version, snapshots, creation time). A configured number of previous
versions is retained; an unchanged snapshot is a no-op. A failed or
schema-incompatible sync **fails closed** and leaves the current DB active.

## Consequences

- Readers always open a fully validated, immutable file read-only (§V2).
- Rollback = repoint `current.json`.
- `purge --rebuild` keeps the current DB active until the rebuilt candidate
  validates (§V20).
- Requires disk for retained versions; acceptable for the local/private scope.
