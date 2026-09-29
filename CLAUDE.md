# CLAUDE.md

Guidance for Claude Code (and other AI agents) working in this repository.

**The canonical guardrails live in [`AGENTS.md`](AGENTS.md) — read it first.**
It carries the architecture rules, the hard invariants (never/always), and the
toolchain. This file adds Claude-Code-specific context only.

## Read before editing

1. `Arknights_MCP_PRD_v0.1_FINAL.md` — product requirements. **PRD Section 18
   (D1–D15) is binding**; changes require an ADR (`docs/adr/`).
2. `AGENTS.md` — the do/never guardrails.

## Layer map (where things go)

| Concern | Location |
|---|---|
| Source adapters | `src/arknights_mcp/sources/` |
| Field allowlist | `src/arknights_mcp/importers/field_policy.py` |
| Parsers / importers | `src/arknights_mcp/importers/` |
| Migrations (SQL) | `src/arknights_mcp/migrations/*.sql` + runner `src/arknights_mcp/db/migrations.py` |
| Data access (read-only) | `src/arknights_mcp/db/`, `db/repositories/` |
| Analyzers / rules | `src/arknights_mcp/analyzers/`, `analyzers/rules/` |
| Domain services (shared by both transports) | `src/arknights_mcp/services/` |
| MCP schemas / registry | `src/arknights_mcp/models/`, `src/arknights_mcp/mcp/` |
| Transports | `src/arknights_mcp/transports/` |
| Auth / middleware | `src/arknights_mcp/auth/`, `src/arknights_mcp/middleware/` |

## Working rhythm

- Run the verification gate before committing:

  ```bash
  uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
  ```

## Non-negotiables (quick reference — full list in `AGENTS.md`)

- No query-time network from MCP tools. Read-only, parameterized SQLite only.
- Every fact carries region + provenance; every observation carries rule_id +
  evidence + confidence + limitations + analyzer_version.
- Never commit raw snapshots, prebuilt databases, or game content (art, audio,
  story, voice, wiki prose, full announcements).
- One shared core, two transports — never duplicate domain logic.
- Admin ops are CLI-only, never MCP tools.
