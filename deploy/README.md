# Deployment examples

Reference deployments for the read-only Arknights Intelligence MCP. The bulk of
this directory covers the **private remote** transport (§I.api; §T55) — three
interchangeable fronts for one posture:

- [`systemd/`](systemd/) — a hardened service unit for a bare-metal / VM host.
- [`nginx/`](nginx/) — the TLS-terminating reverse proxy.
- [`docker/`](docker/) — a code-only image + a compose stack (app + nginx).

The Docker front also serves the local **`stdio`** transport from the same image
— see [Local `stdio` in the same image](#local-stdio-in-the-same-image) below.

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
| `ARKNIGHTS_MCP_ACCOUNT_DB_URL` | Personal account roster database (ADR 0020): read-only role URL for `serve`, writer role URL on the machine that runs `account login` / `account sync`; unset means the three account tools answer `database_unavailable` |

Each example ships an `arknights-mcp.env.example` with placeholders only. Copy it,
fill in real values, and keep it out of git (`chmod 600` for the systemd file).

## Data is mounted, never bundled (§V16)

The Docker image is **code-only**: it bundles the project code + locked deps and
**no** data. The promoted SQLite build is supplied at runtime as a **read-only**
mounted volume (§V2/§V16). `.dockerignore` bars `data/`, `*.sqlite`, and snapshots
from the build context so they cannot leak into a layer. Build a database first
with the admin CLI (`import` / `sync`) — it is a separate step (§V28); the server
never fetches source data at query time (§V1).

## Local `stdio` in the same image

One shared core, two transports (§V14) — and one image for both (§T215). The
Dockerfile keeps only the console script and `--config /app/config.toml` in
`ENTRYPOINT`; the transport lives in `CMD`, so a `docker run` argument list
overrides it:

```bash
docker build -f deploy/docker/Dockerfile -t arknights-mcp .

docker run --rm -i --user "$(id -u):$(id -g)" \
  -v "$PWD/data:/app/data:ro" \
  -v "$PWD/config.toml:/app/config.toml:ro" \
  arknights-mcp serve --transport stdio
```

Or as a compose service, which `up` never starts because it sits behind the
`stdio` profile (a stdio server owns a pipe and exits at EOF — it is not a
listener to bring up):

```bash
ARKNIGHTS_MCP_UID=$(id -u) ARKNIGHTS_MCP_GID=$(id -g) \
  docker compose -f deploy/docker/docker-compose.yml run --rm -T mcp-stdio
```

Four points, each of which silently breaks the transport if missed:

| Requirement | Why |
|---|---|
| `-i` (compose: `stdin_open: true`) | No stdin ⇒ EOF before `initialize` ⇒ the server exits and the client reports it died. |
| **No** `-t` (compose: `-T`) | A TTY merges stderr into stdout and rewrites newlines, corrupting the JSON-RPC framing stdout carries (§V13). `compose run` allocates one by default. |
| `--user` = the uid that owns `data/` | The image runs non-root as uid 999 (§V1/§V2) and reads the build through host permissions, but `import` writes `data/current.json` and the `.sqlite` builds mode `600` owned by the operator who ran it. Mismatch ⇒ `PermissionError` on `data/current.json` ⇒ every tool answers `internal_error`. Mounts stay `:ro`, so this is read access, never a write path. |
| No env file, no OIDC | A local pipe has no bind, no bearer, and no §V9 gate. The compose `mcp` service's `env_file` is `required: false` for exactly this reason — otherwise a missing OIDC file would fail validation of the whole file and block `run mcp-stdio`. Remote serving without OIDC still fails closed at startup (§V9/§V40); the gate is in the app, not in whether a file exists. |

Everything else is unchanged from the remote posture: the image stays code-only
(§V16), the build arrives on a read-only mount (§V2), and `import` / `sync` remain
host-side admin CLI steps (§V28).

## Personal account roster database (ADR 0020)

Optional, and separate from everything above: a personal Yostar (en) account
roster, synced by a CLI-only `account` command group and read by three
read-only MCP tools. Game-data builds stay exactly as they are — SQLite under
`data/builds/`, built by `sync` on this host. Only the personal roster moves to
PostgreSQL, and only the owner's own machine ever logs in to Yostar.

**Server (the host running `serve`).**

```bash
cp deploy/docker/account-db.env.example deploy/docker/account-db.env
# set three passwords in account-db.env, then:
docker compose -f deploy/docker/docker-compose.yml --profile account up -d account-db
```

Put the reader role's URL in `arknights-mcp.env` (compose: host `account-db:5432`;
systemd: `127.0.0.1:5433`), then restart `mcp`. With the database down or the
variable unset, the three account tools answer `database_unavailable`; every
other tool is unaffected.

**Sync machine (the owner's own PC, not this host).** Clone the repository,
`uv sync`, `cp .env.example .env` with the writer role's password, then in a
second terminal keep open:

```bash
ssh -N -L 15432:127.0.0.1:5433 <user>@<server>
```

With that forward open: `uv run --env-file .env arknights-mcp account login`
once, and `uv run --env-file .env arknights-mcp account sync` — with the game
fully closed — whenever the roster should refresh. `account status`, `logout`
and `purge` work the same way.

Why the split: the Yostar login must come from the machine the owner normally
plays from, never from this server, so an account ban tied to server traffic
can't happen. The SSH forward carries only database traffic — no Yostar
request ever crosses it. This server never holds the writer role's URL or the
Yostar session token; it only ever reads the roster through the SELECT-only
reader role.

## Pre-auth flood protection is the proxy's job (§V11)

The app's per-principal rate/concurrency limits (§V11) only meter **validated**
principals. Unauthenticated request storms never reach a per-principal bucket, so
capping them is the reverse proxy's responsibility — the nginx example carries
`limit_req` / `limit_conn` zones for exactly that.

## OAuth discovery must reach the app unauthenticated (§V45)

For interactive login (`claude mcp login`) the app publishes RFC 9728
protected-resource metadata at `/.well-known/oauth-protected-resource` (and the
`/mcp`-suffixed form) **without** a bearer, so an MCP OAuth client can discover the
authorization server from a `401`. A proxy that forwards only `/mcp` would `404`
that path — the nginx example therefore adds a `location` block forwarding the
well-known prefix to the app (still capped by the pre-auth ingress zones). The
metadata advertises the **issuer only**; the client fetches authorization-server
metadata straight from your provider, so the app never proxies it (§V1). `/mcp`
itself stays bearer-gated. See [`../docs/clients/remote.md`](../docs/clients/remote.md).

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
  [`../docs/clients/codex.md`](../docs/clients/codex.md) — the local `stdio` setup,
  including the ready-to-paste Docker client entries (Option C in each).
- [`../docs/adr/0006-oauth-oidc-remote-auth.md`](../docs/adr/0006-oauth-oidc-remote-auth.md)
  — the fail-closed OAuth/OIDC decision.
- SPEC §V9/§V40 (auth posture), §V10 (bearer validation), §V11 (limits),
  §V12/§I.env (env-only secrets), §V16 (code-only distribution).
