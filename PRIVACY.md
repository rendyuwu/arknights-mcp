# Privacy

This document explains what this project does and does not process, store, and
log. It reflects the founder-approved decisions (D10, D15) and the privacy and
logging rules in the PRD (Section 17.5) and [`SPEC.md`](SPEC.md).

## Personal account sync (opt-in, CLI-only; ADR 0020)

This project can optionally sync one owner-controlled Yostar (`en`) account,
driven entirely by the CLI `account` command group (`login`, `sync`,
`status`, `logout`, `purge`). Nothing about this is automatic: it never runs
on a schedule and is never exposed as an MCP tool. This is the one exception
to this project's no-game-credentials posture (SPEC §V15): the resulting
Yostar session token functions as a game credential, and its storage is
deliberately scoped to the machine that requested it.

**What is stored:**

- The Yostar session (account uid + a long-lived token) is written to
  `$XDG_CONFIG_HOME/arknights-mcp/yostar_session.json`, file mode 600, kept
  only on the machine that runs `account login` / `account sync`.
- The allowlisted roster fields — owned operators with elite, level,
  potential, skill level, per-skill mastery, unlocked modules and levels,
  the equipped module, the current skin, owned skins, and inventory — are
  kept in the owner's own PostgreSQL database, table `account_roster`, one
  row per server.

**What is never stored:** the login email address, the one-time code, the
game player uid, the account nickname, friends, and squads.

**Deletion:** `account logout` deletes the saved session token only.
`account purge` deletes the synced roster row and the session token.

**Retention:** the synced roster is kept until the next successful
`account sync` or until `account purge` is run; there is no separate
expiry.

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
