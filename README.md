# Arknights Intelligence MCP

Read-only Arknights intelligence exposed over the [Model Context Protocol
(MCP)](https://modelcontextprotocol.io) from **one shared application core**
across two transports — local `stdio` and a private, OAuth/OIDC-protected
Streamable HTTP endpoint — backed by **versioned SQLite snapshots**.

It answers structured stage/enemy and operator/module questions with region
tagging, source provenance, and deterministic, evidence-backed analysis.
User-facing tools query SQLite only; they **never** fetch upstream data at
query time.

> **⚠️ Unofficial fan project.** This is an unofficial, non-commercial,
> fan-made tool. It is **not affiliated with, endorsed by, or sponsored by
> Hypergryph, Yostar, the maintainers of any upstream data source, Anthropic,
> or OpenAI.** "Arknights" and all related names, logos, characters, artwork,
> audio, story text, and other game content are trademarks and/or copyrights
> of their respective rights holders. See [`NOTICE`](NOTICE).

## Status

v0.1 (private alpha, in development). Private and non-commercial. Public,
multi-tenant hosting is explicitly out of scope and gated behind a separate
readiness review — it cannot be enabled by a single configuration flag.

## What it does (v0.1)

- **Stage & enemy intelligence** — stage metadata, enemies, routes, waves,
  tiles, spawns, and a deterministic stage-threat analyzer with evidence.
- **Operator & module intelligence** — operators, skills, talents, modules,
  and conservative, capability-based observations.
- Regions: `en` and `cn`. Region-specific data is never silently mixed.

## What it does **not** do

- No query-time downloading or scraping — data comes from imported/synced
  snapshots only.
- Optional, CLI-only personal account sync (Yostar en; ADR 0020). No other
  game login or credential handling.
- No squad optimizer, combat simulator, or banner/gacha planning.
- No bundled raw snapshots or prebuilt databases in releases.

## Data, licensing & policies

The Apache-2.0 [`LICENSE`](LICENSE) covers **project code only**. Imported
game data and game content are separately governed — see [`NOTICE`](NOTICE).

Please read, in particular:

- **[`DATA_SOURCES.md`](DATA_SOURCES.md)** — the source registry: owner, URL,
  purpose, fields consumed, region, license/permission posture, attribution,
  enabled state, and last review for every source.
- **[`DATA_POLICY.md`](DATA_POLICY.md)** — field allowlist, excluded content,
  no-bulk-export policy, and provenance requirements.
- **[`TAKEDOWN_POLICY.md`](TAKEDOWN_POLICY.md)** — how to request attribution,
  correction, source exclusion, or removal, and the disable/purge/rebuild
  procedure.
- **[`PRIVACY.md`](PRIVACY.md)** — remote processing, minimal logging,
  retention, and the no-credentials posture.
- **[`SECURITY.md`](SECURITY.md)** — how to report vulnerabilities and the
  runtime security posture.

## Quickstart (local `stdio`)

> The CLI subcommands land across milestones M1–M2; this section is the
> intended shape.

```bash
uv sync
# Build a local database from an approved local snapshot:
uv run arknights-mcp import --server en --source-path ./snapshot/en
# Serve over stdio for a local MCP host (Claude Code, Codex, ...):
uv run arknights-mcp serve --transport stdio
```

For client setup, see [`docs/clients/claude-code.md`](docs/clients/claude-code.md)
(Claude Code) and [`docs/clients/codex.md`](docs/clients/codex.md) (OpenAI
Codex). Both wire this server over local `stdio` using the current official MCP
config formats. For the private remote (Streamable HTTP) transport, see
[`docs/clients/remote.md`](docs/clients/remote.md) — validating the authenticated
endpoint against the MCP Inspector, Claude connector, OpenAI API, and ChatGPT
web. See `docs/` for architecture, adding a source, and adding a rule.

## Personal account sync

Optional, CLI-only: pulls your own Yostar (`en`) account roster into a
PostgreSQL database this server reads from (ADR 0020). Off unless you set it
up.

- On the server: `docker compose -f deploy/docker/docker-compose.yml
  --profile account up -d account-db` (see `deploy/README.md`), reader URL in
  `deploy/docker/arknights-mcp.env`.
- On the machine that logs in (home PC): `cp .env.example .env`, fill in the
  writer password, open `ssh -N -L 15432:127.0.0.1:5433 <user>@<server>` in
  another terminal, then `uv run --env-file .env arknights-mcp account login`
  once and `uv run --env-file .env arknights-mcp account sync` whenever the
  roster should refresh; `account status`, `account logout`, `account purge`
  the same way.
- Fully close Arknights first, run `account sync`, then reopen the game; the
  command waits for your confirmation and refuses to run unattended. Never
  run `account login` or `account sync` on the server: the Yostar login must
  come from the machine you normally play from.

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
```

Contributor and coding-agent guardrails live in [`AGENTS.md`](AGENTS.md) and
[`CLAUDE.md`](CLAUDE.md). The founder-approved decisions in the PRD are
binding and any change requires an ADR (see `docs/adr/`).

## License

[Apache-2.0](LICENSE) for project code only. See [`NOTICE`](NOTICE) for the
scope limitation and trademark/attribution notices.
