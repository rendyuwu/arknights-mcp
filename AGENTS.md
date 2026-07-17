# AGENTS.md — Guardrails for coding agents

This file is the **canonical, tool-agnostic** instruction set for any AI coding
agent (Codex, Claude Code, and others) working in this repository.
`CLAUDE.md` points here.

**Before changing files:** read `Arknights_MCP_PRD_v0.1_FINAL.md` (the product
requirements) and `SPEC.md` (the distilled design of record). The
founder-approved decisions in **PRD Section 18 (D1–D15) are binding**; changing
one requires an **ADR** in `docs/adr/` and explicit approval.

## What this project is

A read-only Arknights intelligence **MCP server**: one shared application core,
two transports (local `stdio`, private OAuth/OIDC Streamable HTTP), over
versioned SQLite snapshots. Regions `en` and `cn`. Data is imported/synced via
CLI only; user-facing tools query SQLite and never touch the network.

## Architecture rules (keep these separations)

- One shared core, two transports. **Never duplicate domain logic** across
  transports — both call the same `tool_registry` + services (§V14).
- Keep layers separated: `sources/` (adapters), `importers/field_policy.py`
  (allowlist), `importers/` (parsers), `db/` + `db/repositories/` (data access),
  `analyzers/` (rules), `models/` + `mcp/` (schemas), `transports/`, `auth/`,
  `middleware/`.
- Admin operations (`sync`, `import`, `validate`, `purge`, source management)
  are **CLI-only** and are never exposed as MCP tools (§V28).
- Write tests before broadening parsers. Prefer property tests (Hypothesis) for
  parsing/normalization.

## Never (hard invariants — see `SPEC.md` §V)

- Make an outbound/source-network request from any MCP tool at query time
  (§V1); no query-time download/scrape fallback (§V24).
- Open SQLite writable in an MCP process; run arbitrary SQL, shell, filesystem,
  or a source-download tool; build SQL by string interpolation (§V2).
- Replace the active DB on a failed/incompatible sync, or mutate the active DB
  in place. Promote only after validation, atomically via `current.json`
  (§V3, §V4).
- Emit a factual response without **region + provenance**, or silently mix `en`
  and `cn` (§V5).
- Emit an observation without `rule_id` + evidence + confidence + limitations +
  `analyzer_version` (§V6). Match on typed fields, not NL keywords (§V26).
- Label a recommendation "mandatory" or "universal best"; recommend anything at
  confidence `< 0.5` (report it as a limitation instead) (§V7, §V8).
- Concatenate imported/source text into server instructions or tool
  descriptions; treat imported strings as anything but **untrusted data**;
  generate tool descriptions from imported content; let source data select
  tools, URLs, or admin actions. Strip control characters and cap lengths
  (§V18, PRD 17.6).
- Log full prompts, full tool arguments, response bodies, authorization
  headers, bearer tokens, raw source records, or roster/account data; leak
  stack traces or local paths in errors (§V12, §V23).
- Request, store, or transmit Arknights game credentials or account IDs
  (§V15).
- Commit or ship raw snapshots, prebuilt databases, artwork, audio, story
  scripts, voice lines, wiki/community prose, or full announcement bodies
  (§V16). `data/builds/` and `*.sqlite` are git-ignored.
- Run non-loopback remote without HTTPS + valid OAuth/OIDC; implement
  username/password storage; enable public access via a single flag/env var
  (§V9, §V10; PRD 17.7).
- Provide bulk-dump / DB-download / unbounded pagination / enumeration; exceed
  search default 10 / max 50, page_size max 100, or the 200 KB default response
  cap (§V19, §V22).
- Upgrade the MCP SDK to v2, break a required tool field, or bump
  `schema_version` without an ADR (§V21, §V25).
- Assume reuse permission from a public repo, an attribution offer, or a
  takedown offer (D13).

## Always

- Read SQLite read-only, parameterized queries only; annotate tools read-only
  (§V2).
- Stamp every imported record with `snapshot_id` + `source_path`/key +
  `transform_version` + `record_hash`; apply the explicit field allowlist
  (§V17, §V18).
- Return a typed `status` ∈ {ok, partial, not_found, ambiguous,
  unsupported_server, data_stale, database_unavailable, schema_incompatible,
  analysis_unavailable, internal_error} (§V23).
- Keep the source registry complete per enabled source; keep `get_data_sources`
  free of secrets, local paths, OAuth config, and takedown correspondence
  (§V27).
- For `stdio`: MCP protocol on stdout only, logs on stderr only (§V13).
- Keep the facts / observations / recommendations distinction in the first 512
  chars of the server instructions (PRD 13.1).
- Record an ADR for any change to a founder decision (§18) or a §C constraint.

## Toolchain & workflow

- Python 3.12, managed with `uv`. Dependencies are locked in `uv.lock`
  (`mcp>=1.28.1,<2`, Pydantic v2, SQLAlchemy Core, Ruff, mypy, pytest,
  pytest-cov, Hypothesis).
- Verification gate (run before every commit):

  ```bash
  uv run ruff check .
  uv run ruff format --check .
  uv run mypy
  uv run pytest -q
  ```

- Work is tracked in `SPEC.md` §T (status `.` todo / `~` wip / `x` done). Commit
  per task with message `T<n>: <goal>` and cite the §V invariants touched.
