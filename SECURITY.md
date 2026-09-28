# Security Policy

## Reporting a vulnerability

Please report suspected security or privacy issues **privately**. Do not open a
public issue for a vulnerability.

> Email the monitored security contact for this deployment:
> **`<OPERATOR_SECURITY_CONTACT_EMAIL>`** _(replace with the
> operator-controlled address before deploying)._

Include a description, reproduction steps, and impact. We will acknowledge
receipt, investigate, and coordinate a fix and disclosure timeline with you.
Please give us a reasonable opportunity to remediate before any public
disclosure.

## Supported versions

This is a private-alpha project. Only the latest `0.1.x` line receives security
fixes.

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅        |
| < 0.1   | ❌        |

## Runtime security posture

The project is designed to fail closed and to minimize attack surface
(PRD Section 17; [`SPEC.md`](SPEC.md) §V):

- **Read-only data plane.** SQLite is opened read-only in every MCP process.
  Queries are parameterized only. There is no arbitrary SQL, filesystem, shell,
  or source-download tool (§V2). The personal account roster database
  (ADR 0020) is read through a SELECT-only role whose transactions default to
  read-only.
- **No query-time network.** User-facing MCP tools never reach an upstream
  source; only explicit CLI `sync` / `import` and `account login` /
  `account sync` commands touch allowlisted endpoints (§V1).
- **Bounded outputs.** No bulk-dump endpoint, no database download, no
  unbounded pagination or entity enumeration; search and page-size limits and a
  response-size cap are enforced (§V19, §V22).
- **Admin is CLI-only.** `sync`, `import`, `validate`, `purge`, and source
  management are never exposed as MCP tools (§V28).
- **Untrusted imported data.** Imported strings are treated as data — never
  concatenated into instructions or tool descriptions; control characters are
  stripped and lengths capped (§V18, PRD Section 17.6).
- **Remote authentication.** Non-loopback remote access requires HTTPS and
  valid OAuth/OIDC (issuer, audience, expiry, JWKS signature, required scope).
  Authless non-loopback access is prohibited; username/password storage is
  never implemented; startup fails closed if these are misconfigured (§V9,
  §V10).
- **Rate & resource limits.** The remote transport enforces per-principal rate
  limits, concurrency limits, request timeouts, and request/response caps
  (§V11).
- **Redacted logging.** Default logs never contain full prompts, full tool
  arguments, response bodies, authorization headers, bearer tokens, raw source
  records, or roster/account data (§V12). Errors never expose stack traces or
  local paths (§V23).
- **Account session handling.** `account login` / `account sync` are the only
  CLI-only exception to the no-credentials rule (ADR 0020): the email and
  one-time code are used once and never stored; the resulting Yostar session
  token is stored only in a mode-600 file on the machine that ran the
  command, is never read by any MCP process, and is sent only to the
  allowlisted account endpoints (§V15).

## Synchronization security

CLI sync/import applies a URL/domain allowlist, HTTPS-by-default, redirect and
same-domain limits, per-file and total-download size caps, JSON depth and
record-count limits, safe archive extraction with path-traversal prevention,
checksum manifests, temporary-directory isolation, no shell interpolation of
remote values, and a source kill switch with an audit event
(PRD Section 17.4). CLI `account login` / `account sync` apply a separate
host+path allowlist scoped to the Yostar/Arknights account endpoints
(ADR 0020); no asset or game-data endpoint is reachable through it.

## Account roster store (ADR 0020)

PostgreSQL publishes `127.0.0.1:5433` on the serving host only and is reached
from the sync machine through an SSH local forward; the serving host holds
only the reader credential; the Yostar session file never leaves the sync
machine. `ARKNIGHTS_MCP_ACCOUNT_DB_URL` is env-only, never printed or
logged, and errors name the exception class only. `account login` /
`account sync` refuse to contact Yostar unless the account database is
reachable and writable.
