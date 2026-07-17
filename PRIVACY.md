# Privacy

This document explains what this project does and does not process, store, and
log. It reflects the founder-approved decisions (D10, D15) and the privacy and
logging rules in the PRD (Section 17.5) and [`SPEC.md`](SPEC.md).

## No game credentials, no accounts (SPEC §V15)

- The project **never requests, stores, or transmits Arknights game
  credentials** or player account identifiers.
- There is **no game-server login** and no direct game-server interaction.

## No roster storage in v0.1 (D10)

- Player rosters are **not stored** in v0.1. If roster support is ever added,
  it will be opt-in, with an explicit deletion command and a retention policy.

## Local `stdio` mode

- Runs entirely on your machine. There is **no listening TCP port** and **no
  application authentication**.
- MCP protocol output goes to stdout; logs go to stderr. Local configuration
  and database files use least-privilege filesystem permissions.

## Private remote mode

- **Remote tool arguments are processed by the operator's own server.** If you
  use a private remote deployment, the operator's server receives and processes
  your tool requests.
- Served over **HTTPS only**, with **OAuth/OIDC** authentication required for
  any non-loopback access. There is no anonymous public endpoint.
- **No telemetry by default.**

## Logging (SPEC §V12)

By default, operational logs record only: tool name, status, latency, a
pseudonymous principal ID, result size, and data version. Logs do **not**
record:

- full prompts,
- full tool arguments,
- tool response bodies,
- authorization headers or bearer tokens,
- raw source records,
- roster or account data.

Authentication secrets and bearer tokens are never logged. Diagnostic reports
redact home directories, usernames, tokens, and internal hostnames.

## Retention

Default operational log retention is short and configurable
(`[privacy] operational_log_retention_days`); the recommended maximum for the
private alpha is **14 days**.

## Public service

A public, multi-tenant service is out of scope for v0.1 and cannot be enabled
by a single configuration flag. It requires a separate release profile and
checklist covering permissions/legal review, public privacy/terms, multi-tenant
isolation, abuse response, cost controls, monitoring, and takedown operations
(PRD Section 17.7).
