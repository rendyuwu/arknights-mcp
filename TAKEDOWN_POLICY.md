# Takedown & Source Exclusion Policy

This project maintains a cautious source posture (PRD Section 10.8). We accept
requests concerning **attribution, correction, source exclusion, or removal**,
and we act on them without waiting for a software release.

## Contact

Send requests to the monitored contact address for this deployment:

> **`<OPERATOR_CONTACT_EMAIL>`** _(replace with the operator-controlled,
> monitored address for your instance before deploying)._

Please include the affected `source_id` and/or the specific data domain, and
the nature of the request (attribution, correction, exclusion, or removal).

## What happens when we receive a request

We follow this operational procedure (PRD Section 10.8):

1. **Record** the request and the affected source/domain.
2. **Disable** the adapter using the configuration kill switch.
3. **Stop** future synchronization for that source.
4. **Purge** snapshots and normalized rows attributable **only** to that source.
5. **Rebuild and validate** the SQLite database without the source.
6. **Promote** the rebuilt database atomically (the current database stays
   active until the rebuilt candidate validates — SPEC §V20).
7. **Update** `DATA_SOURCES.md` and the machine-readable registry
   (`config/data_sources.toml`).
8. **Acknowledge** completion to the requester when contact details are
   available.

## Administrative commands

These operations are **CLI-only** and are never exposed as MCP tools
(SPEC §V28):

```bash
arknights-mcp source list
arknights-mcp source disable <source_id>
arknights-mcp source purge <source_id> --rebuild
arknights-mcp source enable <source_id>
```

- `disable` stops new synchronization but keeps the current data.
- `purge <source_id> --rebuild` removes only the rows attributable to that
  source and rebuilds; the current database remains active until the rebuilt
  candidate validates (fail-closed).

Every such action is recorded as a `source_policy_events` row (event types:
`enable`, `disable`, `purge`, `permission_review`, `attribution_change`).

## Posture

Attribution or a takedown offer is **not** treated as permission to reuse
(D13; PRD Section 10.9). We prefer to disable and purge on request rather than
assume continued permission.
