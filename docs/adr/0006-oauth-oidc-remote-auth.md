# ADR 0006: OAuth/OIDC for private remote; fail closed

- **Status:** Accepted
- **Date:** 2026-07-17
- **Founder decision:** D15 (Authentication), D12/D13 (private, non-commercial)
- **Invariants:** §V9, §V10, §V11, §V12

## Context

Personal web access needs authentication compatible with web MCP clients
(Claude, OpenAI API, ChatGPT where supported), without inventing password
storage and without any anonymous public endpoint.

## Decision

The private remote transport is **Streamable HTTP over HTTPS**, protected by
**OAuth/OIDC resource-server validation**: the server validates the bearer
token's **issuer, audience, expiry, JWKS signature, and required scope**. It
**never stores usernames/passwords**. Non-secret OIDC descriptors (`issuer`,
`audience`, `jwks_url`, `required_scopes`) live in config; secrets come from the
environment or a secret manager, never TOML.

**Startup fails closed:** if non-loopback remote mode is enabled without HTTPS
assumptions and valid OAuth/OIDC settings, the server refuses to start (§V9).
Authless non-loopback access is prohibited (loopback dev is the only
exception). The remote transport enforces per-principal rate/concurrency limits,
request timeouts, and request/response caps (§V11), and redacts logs (§V12).

## Consequences

- Standards-based auth that web MCP clients support; no bespoke credential
  store (§V10).
- Misconfiguration cannot silently expose an open endpoint (§V9).
- Local `stdio` remains unauthenticated by design (no port, local trust).
- A public multi-tenant profile is explicitly out of scope and needs a separate
  ADR + readiness checklist (PRD 17.7).
