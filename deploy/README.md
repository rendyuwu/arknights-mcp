# Deployment examples — private remote (Streamable HTTP)

Reference deployments for the read-only Arknights Intelligence MCP **private
remote** transport (§I.api; §T55). Three interchangeable fronts for the same
posture:

- [`systemd/`](systemd/) — a hardened service unit for a bare-metal / VM host.
- [`nginx/`](nginx/) — the TLS-terminating reverse proxy.
- [`docker/`](docker/) — a code-only image + a compose stack (app + nginx).

> These are **examples**, not turnkey production configs. Replace every
> `mcp.example.com`, certificate path, and OIDC value with your own, and review
> against your own threat model before exposing anything.

## The posture (§V9 / §V40)

The server binds **loopback** (`127.0.0.1:8000`) and a **TLS-terminating reverse
proxy** (nginx, Cloudflare Tunnel, ...) is the sole public ingress. A loopback
bind is **not** proof the listener is private — a proxy or tunnel in front serves
the public internet while the app still binds `127.0.0.1`. So the app does **not**
infer "loopback ⇒ trusted": you declare the proxy explicitly in `config.toml`:

```toml
[mcp.remote]
enabled = true
bind_host = "127.0.0.1"
bind_port = 8000
path = "/mcp"
public_base_url = "https://mcp.example.com"   # https:// = HTTPS is in front (§V9)
behind_proxy = true                            # forces the §V9 gate on loopback (§V40)
```

With `behind_proxy = true`, startup **fails closed** (§V9/§V40) unless HTTPS is
declared (`public_base_url` is `https://`) **and** valid OIDC settings are
present — and every `/mcp` request must then carry a bearer the server validates
(§V10). A genuine loopback dev bind (`behind_proxy = false`, no proxy) is the only
authless exception.

## Secrets are env-only (§V12 / §I.env)

The non-secret OIDC descriptors — issuer, audience, jwks_url — and any secrets are
supplied through the **environment**, never committed to `config.toml` or these
files:

| Variable | Meaning |
|---|---|
| `ARKNIGHTS_MCP_OIDC_ISSUER` | Token issuer, exact match incl. trailing slash (§V10) |
| `ARKNIGHTS_MCP_OIDC_AUDIENCE` | The MCP resource-server audience this deployment accepts |
| `ARKNIGHTS_MCP_OIDC_JWKS_URL` | JWKS endpoint; keys selected by `kid` |

Each example ships an `arknights-mcp.env.example` with placeholders only. Copy it,
fill in real values, and keep it out of git (`chmod 600` for the systemd file).

## Data is mounted, never bundled (§V16)

The Docker image is **code-only**: it bundles the project code + locked deps and
**no** data. The promoted SQLite build is supplied at runtime as a **read-only**
mounted volume (§V2/§V16). `.dockerignore` bars `data/`, `*.sqlite`, and snapshots
from the build context so they cannot leak into a layer. Build a database first
with the admin CLI (`import` / `sync`) — it is a separate step (§V28); the server
never fetches source data at query time (§V1).

## Pre-auth flood protection is the proxy's job (§V11)

The app's per-principal rate/concurrency limits (§V11) only meter **validated**
principals. Unauthenticated request storms never reach a per-principal bucket, so
capping them is the reverse proxy's responsibility — the nginx example carries
`limit_req` / `limit_conn` zones for exactly that.

## Quick start (systemd + nginx)

```bash
# 1. Deploy code under /opt/arknights-mcp and build the venv (locked deps):
cd /opt/arknights-mcp && sudo -u arknights-mcp uv sync --frozen --no-dev

# 2. Build + promote a database (admin CLI, §V28) — the server serves this:
sudo -u arknights-mcp .venv/bin/arknights-mcp import --server en --source-path ./snapshot/en

# 3. Secrets (env-only, §V12), root-owned 600:
sudo install -Dm600 deploy/systemd/arknights-mcp.env.example \
  /etc/arknights-mcp/arknights-mcp.env
sudo "$EDITOR" /etc/arknights-mcp/arknights-mcp.env

# 4. Service + proxy:
sudo cp deploy/systemd/arknights-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now arknights-mcp
sudo cp deploy/nginx/arknights-mcp.conf /etc/nginx/sites-available/arknights-mcp.conf
sudo ln -s /etc/nginx/sites-available/arknights-mcp.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## See also

- [`../docs/clients/claude-code.md`](../docs/clients/claude-code.md),
  [`../docs/clients/codex.md`](../docs/clients/codex.md) — the local `stdio` setup.
- [`../docs/adr/0006-oauth-oidc-remote-auth.md`](../docs/adr/0006-oauth-oidc-remote-auth.md)
  — the fail-closed OAuth/OIDC decision.
- SPEC §V9/§V40 (auth posture), §V10 (bearer validation), §V11 (limits),
  §V12/§I.env (env-only secrets), §V16 (code-only distribution).
