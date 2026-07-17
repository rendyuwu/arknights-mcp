# ADR 0001: Dual transport over one shared application core

- **Status:** Accepted
- **Date:** 2026-07-17
- **Founder decision:** D1 (Deployment target), D2 (Language)
- **Invariants:** §V14, §V13, §V22, §V23

## Context

The product must serve both a local host (Claude Code, Codex, other local MCP
hosts) and personal web access, without committing to a public multi-tenant
service in v0.1. Two obvious risks: (a) building two servers that drift apart,
and (b) leaking domain logic into transport code.

## Decision

Implement **one shared application core** — a single `tool_registry` plus a
`services/` layer over read-only SQLite — and expose it through **two thin
transports**:

- local `stdio` (`transports/stdio.py`): no listening port, no app auth, MCP
  protocol on stdout, logs on stderr;
- private Streamable HTTP (`transports/streamable_http.py`): HTTPS, OAuth/OIDC,
  same registry and services.

Transports contain only protocol/session wiring. All domain logic lives in the
shared core. The same DB + same input yields an identical domain result on
either transport.

## Consequences

- No duplicated domain logic (§V14); parity is testable (local↔remote parity
  tests, T61).
- Adding a tool means adding it once to the registry.
- `stdio` keeps stdout clean for protocol only (§V13).
- Reversal (e.g., diverging cores per transport) would require a new ADR.
