# Connect from OpenAI Codex (local `stdio`)

Wire the read-only Arknights Intelligence MCP into the [OpenAI Codex
CLI](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) as a local `stdio`
server. The config formats below are the current official ones (verified
2026-07).

> This server is **read-only**. It never fetches upstream data at query time
> (SPEC §V1); it serves whatever build you have already promoted locally.
> Building and refreshing data is a **separate admin-CLI step** (§V28) — see
> [Prerequisite](#prerequisite) first.

## Prerequisite: build a database

`serve` opens the *promoted* SQLite build strictly read-only. Build one from an
approved local snapshot (or an allowlisted `sync`) before you point Codex at
it, then confirm it promoted:

```bash
uv sync
uv run arknights-mcp import --server en --source-path ./snapshot/en
uv run arknights-mcp status        # shows the active snapshot + schema version
```

`import`, `sync`, `validate`, `status`, and `source` are admin-only CLI
commands, never MCP tools (§V28). Run them yourself before starting the server.
To refresh, run them and then **restart** the server: it opens the promoted
build once at startup and holds it for the process lifetime, so a build promoted
under a running server is not picked up live.

## Option A — `codex mcp add`

```bash
codex mcp add arknights -- uv run --directory /abs/path/to/arknights-mcp \
  arknights-mcp serve --transport stdio
```

Everything after `--` is the command Codex launches. `uv run --directory` pins
the clone so the server's default `./config.toml` and `./data` resolve
regardless of where Codex is invoked.

## Option B — `~/.codex/config.toml`

Add an `[mcp_servers.<name>]` table. `cwd` is the Codex-native way to pin the
working directory (an alternative to `uv run --directory`):

```toml
[mcp_servers.arknights]
command = "uv"
args = ["run", "arknights-mcp", "serve", "--transport", "stdio"]
cwd = "/abs/path/to/arknights-mcp"
# First launch may run `uv sync`; raise the init timeout above the 10s default.
startup_timeout_sec = 30
# tool_timeout_sec defaults to 60; stage analysis stays well under it.
```

Supported fields: `command` (required), `args`, `env` (a
`[mcp_servers.<name>.env]` table of key/value pairs to set), `env_vars` (names
to forward from Codex's own environment), `cwd`, `startup_timeout_sec`,
`tool_timeout_sec`.

### Non-default config path

If your config lives elsewhere, add `--config` to `args` (the default is
`./config.toml`, resolved against `cwd`). `--config` is a top-level flag, so it
goes **before** the `serve` subcommand:

```toml
[mcp_servers.arknights]
command = "uv"
args = [
  "run", "arknights-mcp", "--config", "/abs/path/to/config.toml",
  "serve", "--transport", "stdio",
]
cwd = "/abs/path/to/arknights-mcp"
```

## Verify

```bash
codex mcp list           # arknights should appear
```

Inside the Codex TUI, type `/mcp` to list connected servers and their tools,
then ask something a promoted build can answer, e.g. *"analyze stage 4-4"*.

## Troubleshooting

- **Every query is `not_found` / `data_stale`.** No build is promoted for that
  region, or it is stale. Run `uv run arknights-mcp status`; refresh with
  `import` or `sync` (§V24 — the server never downloads on demand), **then
  restart the server** — it holds the build it opened at startup for the process
  lifetime, so a fresh promote under a running server is not picked up until you
  restart it.
- **Server times out on startup.** First launch can trigger `uv sync`. Run `uv
  sync` once beforehand and/or raise `startup_timeout_sec`.
- **`config.toml` / `data/` not found.** `cwd` (or `uv run --directory`) is
  unset or wrong; point it at the clone.
- **Stray text in the transport.** The server writes the MCP JSON-RPC stream to
  **stdout** and all operational notices to **stderr** (§V13); don't wrap the
  command in anything that prints to stdout.

## See also

- [`claude-code.md`](claude-code.md) — the same server from Claude Code.
- [`../../README.md`](../../README.md) — project overview and data policy.
- SPEC §V1/§V2/§V13/§V28 — the read-only, CLI-only, stdout/stderr guardrails
  this setup relies on.
