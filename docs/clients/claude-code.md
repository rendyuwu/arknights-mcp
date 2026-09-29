# Connect from Claude Code (local `stdio`)

Wire the read-only Arknights Intelligence MCP into [Claude
Code](https://code.claude.com/docs/en/mcp) as a local `stdio` server. The
config formats below are the current official ones (verified 2026-07).

> This server is **read-only**. It never fetches upstream data at query time;
> it serves whatever build you have already promoted locally.
> Building and refreshing data is a **separate admin-CLI step** — see
> [Prerequisite](#prerequisite) first.

## Prerequisite: build a database

`serve` opens the *promoted* SQLite build strictly read-only. If you have never
built one, the server has nothing to answer from. Build one from an approved
local snapshot (or an allowlisted `sync`), then confirm it promoted:

```bash
uv sync
uv run arknights-mcp import --server en --source-path ./snapshot/en
uv run arknights-mcp status        # shows the active snapshot + schema version
```

`import`, `sync`, `validate`, `status`, and `source` are admin-only CLI
commands. They are **not** exposed as MCP tools, so you run them yourself
before starting the server. To refresh, run them and then **restart** the
server: it opens the promoted build once at startup and holds it for the process
lifetime, so a build promoted under a running server is not picked up live.

## Option A — project scope (`.mcp.json`, recommended)

Project scope commits the server to the repo so anyone who clones it gets the
same config. From the repository root:

```bash
claude mcp add --transport stdio --scope project arknights \
  -- uv run arknights-mcp serve --transport stdio
```

That writes a `.mcp.json` at the repo root:

```json
{
  "mcpServers": {
    "arknights": {
      "command": "uv",
      "args": ["run", "arknights-mcp", "serve", "--transport", "stdio"],
      "env": {}
    }
  }
}
```

An entry with `command`/`args` and **no** `type` is read as a `stdio` server.
Claude Code launches it with the working directory set to the repo root, so
`./config.toml` and `./data` resolve as expected. Project-scoped servers from
`.mcp.json` require your approval the first time — run `claude` interactively
and accept the workspace-trust prompt.

## Option B — user scope (available across all projects)

If you want the server available from any directory, pin the clone location
with `uv run --directory` so `config.toml` and `data/` still resolve:

```bash
claude mcp add --transport stdio --scope user arknights \
  -- uv run --directory /abs/path/to/arknights-mcp \
     arknights-mcp serve --transport stdio
```

User-scoped servers are stored in `~/.claude.json`. Use `--scope local` (the
default) instead if you want it only in the current project, only for you.

### Non-default config path

If your config lives elsewhere, pass an absolute `--config` (the default is
`./config.toml`, resolved against the launch directory). `--config` is a
top-level flag, so it goes **before** the `serve` subcommand:

```bash
claude mcp add --transport stdio --scope user arknights \
  -- uv run --directory /abs/path/to/arknights-mcp \
     arknights-mcp --config /abs/path/to/config.toml serve --transport stdio
```

## Option C — run it in Docker (`stdio`)

The deploy image (`deploy/docker/Dockerfile`) serves either transport: the
transport lives in `CMD`, so appending `serve --transport stdio` to `docker run`
overrides the streamable-http default. Build it once from the repo root:

```bash
docker build -f deploy/docker/Dockerfile -t arknights-mcp .
```

Then run it as an MCP server on a pipe:

```bash
docker run --rm -i --user "$(id -u):$(id -g)" \
  -v /abs/path/to/arknights-mcp/data:/app/data:ro \
  -v /abs/path/to/arknights-mcp/config.toml:/app/config.toml:ro \
  arknights-mcp serve --transport stdio
```

Register that with Claude Code:

```bash
claude mcp add --transport stdio --scope user arknights \
  -- docker run --rm -i --user 1000:1000 \
     -v /abs/path/to/arknights-mcp/data:/app/data:ro \
     -v /abs/path/to/arknights-mcp/config.toml:/app/config.toml:ro \
     arknights-mcp serve --transport stdio
```

Four things about that command line are load-bearing:

- **`-i` is required.** Without it the container gets no stdin, so the server
  reads EOF immediately and exits before the client's `initialize` arrives.
- **Never pass `-t`.** A TTY merges stderr into stdout and rewrites newlines,
  which corrupts the JSON-RPC framing stdout carries. `-i` alone is right.
- **`--user` with your own uid, not root.** The image runs as its non-root
  `arknights` user (uid 999) and reads the build through host file permissions —
  but `import` writes `data/current.json` and the `.sqlite` builds mode `600`
  owned by whoever ran it, so uid 999 gets `PermissionError` and every tool
  answers `internal_error`. Pass `--user "$(id -u):$(id -g)"`. In a `.mcp.json` /
  `claude mcp add` entry there is no shell to expand that, so write the numbers
  (`--user 1000:1000`); check yours with `id -u`. The mounts stay `:ro`, so this
  grants read access, never a write path.
- **No `--config` flag.** The image's `ENTRYPOINT` already pins
  `--config /app/config.toml`; the mount above is what decides its contents.

The image is **code-only**: it contains no database. The promoted build
arrives through the read-only `data` mount, so you still build it on the host
first (see [Prerequisite](#prerequisite)) and a rebuilt image never carries game
data. No OIDC or env file is involved — a stdio pipe has no bind and no bearer.

For a smoke test rather than a client registration, `deploy/docker/docker-compose.yml`
carries the same thing as a `stdio`-profile service:

```bash
ARKNIGHTS_MCP_UID=$(id -u) ARKNIGHTS_MCP_GID=$(id -g) \
  docker compose -f deploy/docker/docker-compose.yml run --rm -T mcp-stdio
```

`-T` disables the TTY that `compose run` allocates by default, for the reason
above. Compose's own progress lines go to stderr, so stdout stays protocol-only.

## Verify

```bash
claude mcp list          # arknights should show as connected
claude mcp get arknights # inspect the resolved command/args/scope
```

Inside a Claude Code session, `/mcp` lists connected servers and their tools.
Ask something a promoted build can answer, e.g. *"analyze stage 4-4"* or
*"get operator SilverAsh"*.

## Troubleshooting

- **Server connects but every query is `not_found` / `data_stale`.** No build
  is promoted for that region, or it is stale. Run `uv run arknights-mcp
  status`; build/refresh with `import` or `sync` (the server never
  downloads on demand to fill the gap), **then restart the server** — it holds
  the build it opened at startup for the process lifetime, so a fresh promote
  under a running server is not picked up until you restart it.
- **`command: expected string` / server skipped.** The JSON entry is
  malformed. For a `stdio` server keep `command`/`args` and omit `type`.
- **`config.toml` / `data/` not found.** The launch directory is wrong. Use
  project scope (runs at repo root) or `uv run --directory <clone>` in user
  scope.
- **Stray text in the transport.** The server writes the MCP JSON-RPC stream to
  **stdout** and all operational notices to **stderr**; don't wrap the
  command in anything that prints to stdout.
- **(Docker) every tool answers `internal_error`, or `arknights-mcp status` in
  the container raises `PermissionError: [Errno 13] Permission denied:
  'data/current.json'`.** The container uid cannot read the mounted build. Add
  `--user "$(id -u):$(id -g)"` (numeric in a `.mcp.json` entry) — see
  [Option C](#option-c--run-it-in-docker-stdio). The tool-level error is
  deliberately redacted, so the container's **stderr** (or a `status` run) is
  where you see the cause.
- **(Docker) the container exits at once and the client reports the server
  died.** `-i` is missing, so stdin is closed and the server sees EOF before
  `initialize`. Never substitute `-t` for it.

## See also

- [`codex.md`](codex.md) — the same server from OpenAI Codex.
- [`../../README.md`](../../README.md) — project overview and data policy.
- Read-only, CLI-only, stdout/stderr guardrails this setup relies on.
