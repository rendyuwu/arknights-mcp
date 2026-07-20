# Validate the private remote (Streamable HTTP)

Manual validation runbook for the read-only Arknights Intelligence MCP **private
remote** transport against real MCP hosts: the MCP Inspector, Claude's custom
connector, the OpenAI API, and ChatGPT web. The steps below are the current
official flows (verified 2026-07).

> This server is **read-only** and never fetches upstream data at query time
> (§V1); it serves whatever build you have already promoted locally (§V28). The
> remote transport adds **auth**: every `/mcp` request must carry a bearer the
> server validates (§V10), and the server must sit behind HTTPS (§V9/§V40).

The one machine-checkable claim these hosts rest on — an authenticated request
over the wire is served by the *same shared core* `stdio` serves (§V14) — is
covered offline by `tests/remote/test_remote_authenticated_e2e.py`. This runbook
is the human half: it needs a public HTTPS endpoint, a live OIDC provider, and
host accounts, so it cannot run in CI.

## Prerequisites

1. **A promoted build.** `serve` opens the promoted SQLite build strictly
   read-only; build one first (admin CLI, §V28), then confirm:

   ```bash
   uv sync
   uv run arknights-mcp import --server en --source-path ./snapshot/en
   uv run arknights-mcp status        # active snapshot + schema version
   ```

2. **A deployed remote endpoint behind HTTPS.** Follow
   [`../../deploy/README.md`](../../deploy/README.md) (systemd + nginx, or
   docker) so the server binds loopback with `behind_proxy = true` and a
   TLS-terminating proxy fronts it at `https://mcp.example.com/mcp`. Startup
   **fails closed** (§V9/§V40) unless HTTPS is declared and valid OIDC settings
   are present.

3. **An OIDC provider + a test bearer.** The server validates RS256 tokens
   against your provider's JWKS by `kid`, matching `iss`/`aud`/`exp` and the
   required scope (§V10). The OIDC descriptors are **env-only** (§I.env/§V12):

   | Variable | Meaning |
   |---|---|
   | `ARKNIGHTS_MCP_OIDC_ISSUER` | token issuer, exact match incl. trailing slash |
   | `ARKNIGHTS_MCP_OIDC_AUDIENCE` | the resource-server audience this deployment accepts |
   | `ARKNIGHTS_MCP_OIDC_JWKS_URL` | JWKS endpoint; keys selected by `kid` |

   Mint a machine-to-machine token for the audience above, granted the required
   scope (default `arknights:read`). With Auth0, that is a client-credentials
   grant against the API you registered as the audience.

## Smoke check (before any host)

Confirm auth is live and a bearer works, with `curl`. A request with **no**
bearer must be refused with a `401` challenge:

```bash
curl -sS -i https://mcp.example.com/mcp \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'
# → HTTP/1.1 401 Unauthorized
#   WWW-Authenticate: Bearer error="invalid_token", ...
```

A request **with** a valid bearer reaches the protocol (the SDK negotiates the
session; a raw `initialize` without the full handshake may still error at the
MCP layer, but it must not be a `401`):

```bash
curl -sS -i https://mcp.example.com/mcp \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
# → HTTP/1.1 200 (initialize result), not 401
```

### OAuth discovery (for interactive login)

For clients that log in interactively (`claude mcp login`) rather than carrying a
pre-issued bearer, the server publishes **RFC 9728 protected-resource metadata**
*unauthenticated* (§V45), so the client can discover the authorization server from
a `401` without a hand-pasted token. The discovery document is reachable with **no**
`Authorization` header:

```bash
curl -sS https://mcp.example.com/.well-known/oauth-protected-resource
# → 200 {"resource":"https://mcp.example.com/mcp",
#        "authorization_servers":["https://YOUR_TENANT.us.auth0.com/"],
#        "scopes_supported":["arknights:read"],
#        "bearer_methods_supported":["header"]}
```

The metadata advertises the **issuer only**; the client fetches the
authorization-server metadata straight from your OIDC provider (Auth0's own
`.well-known`), so this server never proxies it and makes no query-time network call
(§V1). The `401` on `/mcp` carries a `resource_metadata="…"` hint pointing back at
this document (RFC 9728 §5.1). Only the two well-known paths bypass auth — `/mcp`
itself stays bearer-gated (§V10).

## MCP Inspector

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) drives a
server interactively over a transport.

```bash
npx @modelcontextprotocol/inspector
```

In the UI: set **Transport** to *Streamable HTTP*, **URL** to
`https://mcp.example.com/mcp`, and add an **Authorization** header
`Bearer <token>`. Then:

- **Connect** → `initialize` succeeds; server name is `arknights-mcp` and the
  shared instructions appear.
- **List Tools** → exactly the seven read-only tools: `search_entities`,
  `search_stages`, `get_stage`, `get_enemy`, `get_operator`,
  `compare_operator_modules`, `analyze_stage`. Each shows `readOnlyHint: true`.
- **Call Tool** `get_enemy` with `{"server":"en","game_id":"enemy_1007_slime"}`
  → a typed `ok` envelope with `provenance[0].server == "en"` (§V5/§V23).

Record: connected, seven tools, an `ok` call — identical to what `stdio` serves
(§V14). Clear the bearer and confirm the connection is refused.

## Claude Code (`claude mcp login`)

With OAuth discovery live (above), the Claude Code CLI can bootstrap auth itself —
no hand-pasted bearer:

```bash
claude mcp add --transport http arknights https://mcp.example.com/mcp
claude mcp login arknights   # opens the browser OAuth flow against your provider
```

The CLI hits `/mcp`, reads the `401`'s `resource_metadata` hint, fetches the
protected-resource metadata, then the authorization-server metadata from your issuer,
and runs the OAuth flow for a bearer scoped `arknights:read`. Two provider-side
prerequisites (Auth0 dashboard, one-time):

- **Dynamic Client Registration** enabled, *or* a pre-registered client whose id the
  CLI is configured with — MCP interactive login expects one of these.
- The **API** you registered as the audience (`https://mcp.example.com/mcp`) exposes
  the `arknights:read` permission/scope, and the login callback URL is allowed.

If your provider allows neither DCR nor a usable public client, fall back to the
pre-issued M2M bearer flow (`claude mcp add … --header "Authorization: Bearer <token>"`).

## Claude custom connector

Add the endpoint as a
[custom connector](https://support.anthropic.com/en/articles/11175166-about-custom-connectors-remote-mcp)
(Settings → Connectors → Add custom connector). Supply the remote MCP URL
`https://mcp.example.com/mcp` and complete the OAuth flow your provider exposes
so Claude obtains a bearer for the configured audience/scope. Then, in a chat
with the connector enabled, ask something a promoted build answers — e.g.
*"analyze stage 4-4"* or *"get enemy Originium Slime"* — and confirm the tool
call returns facts with region provenance, not an auth error.

## OpenAI API (Responses / remote MCP tool)

Point the [remote MCP tool](https://platform.openai.com/docs/guides/tools-remote-mcp)
at the endpoint, passing the bearer as an authorization header:

```bash
curl https://api.openai.com/v1/responses \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H 'content-type: application/json' \
  -d '{
    "model": "gpt-5",
    "tools": [{
      "type": "mcp",
      "server_label": "arknights",
      "server_url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer '"$ARKNIGHTS_TOKEN"'" },
      "require_approval": "never"
    }],
    "input": "Analyze Arknights stage 4-4 and list the enemy threats."
  }'
```

Confirm the response shows the model listed the tools and invoked one (e.g.
`analyze_stage`), returning evidence-backed observations — not an authorization
failure. `$ARKNIGHTS_TOKEN` is the resource-server bearer for **this** MCP; it is
distinct from your OpenAI API key.

## ChatGPT web (if your workspace supports it)

Custom/remote MCP connectors in ChatGPT are gated to certain plans and
workspaces. **If** yours exposes it (Settings → Connectors → Add), add
`https://mcp.example.com/mcp`, complete the OAuth flow for the bearer, then ask a
promoted-build question and confirm a tool call returns facts. If your workspace
has no custom-connector option, record it as unsupported and rely on the MCP
Inspector + Claude connector + OpenAI API checks above.

## Troubleshooting

- **`401` with a valid-looking token.** Check `iss` (exact, trailing slash),
  `aud`, and `exp`; the server requires `exp`/`iat`/`iss`/`aud` and RS256 (§V10).
  A wrong audience or issuer is a `401`; a missing scope is a `403` with a
  `scope=` hint.
- **Startup refused (`ConfigError`).** The §V9/§V40 gate: `public_base_url` must
  be `https://` **and** valid OIDC settings must be present whenever auth is
  required (a non-loopback bind, or loopback with `behind_proxy = true`).
- **Every query is `not_found` / `data_stale`.** No build is promoted for that
  region, or it is stale. Refresh with `import`/`sync` (§V24 — the server never
  downloads on demand) and **restart** the server; it holds the build it opened
  at startup for the process lifetime.
- **Connector reaches the server but no tools appear.** The bearer authenticates
  but the session did not initialize — confirm the client speaks Streamable HTTP
  (not the deprecated SSE transport) and that the proxy forwards
  `text/event-stream` responses unbuffered.

## See also

- [`../../deploy/README.md`](../../deploy/README.md) — the HTTPS + OIDC remote
  deployment this validates.
- [`claude-code.md`](claude-code.md), [`codex.md`](codex.md) — the local `stdio`
  setup.
- [`../adr/0006-oauth-oidc-remote-auth.md`](../adr/0006-oauth-oidc-remote-auth.md)
  — the fail-closed OAuth/OIDC decision.
- SPEC §V9/§V40 (auth posture), §V10 (bearer validation), §V11 (limits),
  §V12/§I.env (env-only secrets), §V14 (one shared core, both transports).
