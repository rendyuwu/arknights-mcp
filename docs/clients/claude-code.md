# Connect from Claude Code (local `stdio`)

Wire the read-only Arknights Intelligence MCP into [Claude
Code](https://code.claude.com/docs/en/mcp) as a local `stdio` server. The
config formats below are the current official ones (verified 2026-07).

> This server is **read-only**. It never fetches upstream data at query time
> (SPEC §V1); it serves whatever build you have already promoted locally.
> Building and refreshing data is a **separate admin-CLI step** (§V28) — see
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
commands. They are **not** exposed as MCP tools (§V28), so you run them yourself
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
  status`; build/refresh with `import` or `sync` (§V24 — the server never
  downloads on demand to fill the gap), **then restart the server** — it holds
  the build it opened at startup for the process lifetime, so a fresh promote
  under a running server is not picked up until you restart it.
- **`command: expected string` / server skipped.** The JSON entry is
  malformed. For a `stdio` server keep `command`/`args` and omit `type`.
- **`config.toml` / `data/` not found.** The launch directory is wrong. Use
  project scope (runs at repo root) or `uv run --directory <clone>` in user
  scope.
- **Stray text in the transport.** The server writes the MCP JSON-RPC stream to
  **stdout** and all operational notices to **stderr** (§V13); don't wrap the
  command in anything that prints to stdout.

## See also

- [`codex.md`](codex.md) — the same server from OpenAI Codex.
- [`../../README.md`](../../README.md) — project overview and data policy.
- SPEC §V1/§V2/§V13/§V28 — the read-only, CLI-only, stdout/stderr guardrails
  this setup relies on.
