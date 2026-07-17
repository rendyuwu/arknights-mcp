# SPEC

src: Arknights_MCP_PRD_v0.1_FINAL.md + Arknights_MCP_Agent_Execution_Prompt_FINAL.md. founder decisions §18 D1-D15 binding; change → ADR.

## §G GOAL

read-only Python 3.12 Arknights Intelligence MCP. 1 shared core → 2 transports (local `stdio` + private OAuth/OIDC Streamable HTTP). import allowlisted EN/CN snapshots → versioned SQLite. expose stage/enemy + operator/module intel w/ region + provenance + deterministic evidence-backed analysis. ⊥ query-time upstream fetch.

## §C CONSTRAINTS

- Python 3.12 + `uv` (env, lockfile, run).
- MCP Python SDK v1: `mcp>=1.28.1,<2`, exact resolved version in `uv.lock`. SDK v2 pre-release ∴ ⊥ until ADR.
- Pydantic v2. SQLAlchemy Core (⊥ heavy ORM) + explicit migrations (Alembic | small runner). SQLite 3.51.3+. ASGI server for Streamable HTTP.
- pytest + pytest-cov + Hypothesis. Ruff. mypy | pyright.
- Apache-2.0 project code only. `NOTICE` excludes imported data + game content.
- 1 shared core, 2 transports. ⊥ duplicate domain logic across modes.
- separated layers: source adapters, field policy, importers, repositories, analyzers, MCP schemas, transports, auth, middleware.
- data acquire: user local snapshots + explicit allowlisted repo sync only. ⊥ arbitrary URL downloader.
- code-only distribution. ⊥ bundled raw snapshot | prebuilt DB.
- private + noncommercial v0.1. public service = separate readiness gate (out of scope). ⊥ single public-mode flag.
- ⊥ wiki/community prose, art, audio, story, voice, full announcement bodies. announcements metadata-only + disabled v0.1.
- ⊥ game login | roster storage (v0.1) | squad optimizer | combat sim | banner/gacha planning.
- regions v0.1: `en`, `cn`.
- primary source `arknights_assets_gamedata`; CN validator `kengxxiao_gamedata` (CI only); `penguin_statistics` + official news disabled v0.1.
- CI: lint + type + test on Windows + macOS + Linux.

## §I INTERFACES

CLI (`arknights-mcp`, admin-only, may touch allowlisted net):
- cmd: `arknights-mcp sync --server en|cn|all` → build candidate DB from allowlisted source
- cmd: `arknights-mcp import --server en --source-path ./snapshot/en` → build candidate from local snapshot
- cmd: `arknights-mcp validate --database <path>` → integrity/FK/golden report
- cmd: `arknights-mcp status` → active snapshot + schema version
- cmd: `arknights-mcp doctor` → health (versions, DB, sources, transport, config warnings); ⊥ print secrets
- cmd: `arknights-mcp source list | enable <id> | disable <id> | purge <id> --rebuild`
- cmd: `arknights-mcp serve --transport stdio|streamable-http`

MCP tools (read-only, same registry both transports, typed envelope: `schema_version`,`status`,facts/data,`provenance`,`limitations`,`analyzer_version`):
- tool: `search_entities` `get_operator` `compare_operator_modules` `get_enemy` `search_stages` `get_stage` `analyze_stage` `get_data_status` `get_data_sources`

MCP resources (optional, read-only):
- resource: `arknights://operator/{server}/{game_id}` `arknights://enemy/{server}/{game_id}` `arknights://stage/{server}/{stage_id}` `arknights://status/{server}` `arknights://sources`

Files:
- file: `config.toml` (`config.example.toml`) → `[database][sync][analysis][mcp][auth][limits][privacy]`
- file: `config/data_sources.toml` → machine source registry
- file: `data/current.json` → atomic manifest (immutable filename, hash, schema version, snapshots)
- file: `data/builds/<ts>-en-cn.sqlite` → versioned immutable build
- file: policy docs `DATA_SOURCES.md` `DATA_POLICY.md` `TAKEDOWN_POLICY.md` `PRIVACY.md` `NOTICE` `SECURITY.md`

Env + transport:
- env: OAuth/OIDC secrets via env only (⊥ TOML): issuer, audience, jwks_url, required_scopes
- api: Streamable HTTP `POST /mcp` over HTTPS, bind `127.0.0.1:8000` behind reverse proxy
- stdio: MCP protocol → stdout; logs → stderr

## §V INVARIANTS

V1: runtime MCP tool ⊥ outbound source-network request. only CLI sync/import → allowlisted sources.
V2: SQLite opened read-only ∀ MCP process. parameterized SQL only. ⊥ arbitrary SQL | shell | fs | source-download tool.
V3: failed | schema-incompatible sync ⊥ replace current DB (fail closed).
V4: candidate promote only after validate pass: `PRAGMA integrity_check` + `PRAGMA foreign_key_check` + critical-table + row-count + golden. promotion atomic via `current.json`. ⊥ mutate active DB in place.
V5: ∀ factual response → region ∈ {en,cn} + provenance (`snapshot_id`,`imported_at`). en & cn ⊥ silently mixed.
V6: ∀ observation → `rule_id` + evidence + confidence + exceptions/limitations + `analyzer_version`.
V7: recommendations capability-based & conservative. ⊥ "mandatory" | universal-best label (v0.1: never).
V8: confidence `< 0.5` → ⊥ recommendation; report as limitation.
V9: non-loopback remote w/o HTTPS + valid OAuth/OIDC settings → fail startup. authless non-loopback ⊥ (except loopback dev).
V10: remote bearer validated: issuer + audience + expiry + JWKS signature + required scope. ⊥ password/username storage.
V11: remote enforces per-principal rate limit + concurrency limit + request timeout + request cap + response cap.
V12: default logs ⊥ full prompt | full tool args | response body | authorization header | bearer token | raw source record | roster/account.
V13: `stdio` → MCP protocol on stdout only; logs on stderr only.
V14: both transports → same `tool_registry` + services. same DB + same input → identical domain result. ⊥ duplicate domain logic.
V15: ⊥ request | store | transmit Arknights game credentials.
V16: release artifact ⊥ raw snapshot | prebuilt DB | artwork | audio | story script | voice line | wiki/community prose | full announcement body.
V17: ∀ imported record → `snapshot_id` + `source_path`/key + `transform_version` + `record_hash`.
V18: importer applies explicit field allowlist; excludes unused prose. imported string = untrusted data; ⊥ concat into server instructions | tool descriptions; strip control chars; cap length.
V19: ⊥ bulk dump | DB download | unbounded pagination | enumeration → dataset reconstruction. search limit default 10 max 50; page_size max 100.
V20: `disable` → stop new sync, keep current data. `purge --rebuild` → remove only rows attributable to source; current DB active until rebuilt candidate validates.
V21: required tool fields backward-compat within v0.1; additive optional fields preferred; breaking change → `schema_version` bump + ADR.
V22: default tool response `< 200 KB`. large map/spawn → explicit include flag | pagination.
V23: ∀ tool result → typed status ∈ {ok,partial,not_found,ambiguous,unsupported_server,data_stale,database_unavailable,schema_incompatible,analysis_unavailable,internal_error}. error ⊥ stack trace | local path.
V24: absent entity → typed not_found | region_unavailable | data_stale + suggested admin action. ⊥ query-time download/scrape fallback.
V25: `mcp>=1.28.1,<2`; exact resolved version in `uv.lock`. SDK v2 migration → ADR.
V26: analyzer rules on typed fields (⊥ NL keyword match). missing field → reduce confidence | limitation. conflicting source fields → omit conclusion + warn.
V27: source registry complete ∀ enabled source: `source_id` + owner + URL + purpose/domains + regions + license/permission status + attribution + enabled + `last_reviewed` + snapshot commit. `get_data_sources` ⊥ secrets | local fs path | OAuth config | takedown correspondence.
V28: admin ops (sync, import, validate, purge, source mgmt) CLI-only. ⊥ exposed as MCP tool.
V29: importer parse contract verified vs real `arknights_assets_gamedata` schema; ⊥ validate data path solely on synthetic fixture matching parser. real shapes: `enemy_database.json` top-level id-keyed dict → list `{level, enemyData.attributes.<stat>.m_value}` (no `"enemies"` wrapper; `maxHp`/`magicResistance`/`baseAttackTime` ≠ `hp`/`res`/`attackInterval`); `stage_table.levelId` = `Obt/Main/level_main_04-04` (Title-case, no `gamedata/levels/` prefix, no `.json`); level tiles grid-indexed (no `x`/`y`; `passableMask` ≠ `passable`), wave action enemy under `key` ≠ `enemyId`; enemy motion @ `enemy_database` `enemyData.motion.m_value` (handbook no `motionType`).
V30: raw upstream shape ≠ normalized tables → explicit `importers/normalization.py` transform ! bridge. sync/import ! report per-stage import counts; non-empty source yielding 0 levels/tiles/spawns/`stage_enemies` → fail closed (⊥ silent empty build).

## §T TASKS

id|status|task|cites
T1|x|M0 git init repo + .gitignore (exclude data/builds, *.sqlite, snapshots, .venv, __pycache__)|V16
T2|x|M0 scaffold pyproject.toml + package src/arknights_mcp/ layout per PRD §20|-
T3|x|M0 pin deps → uv.lock (mcp>=1.28.1,<2, pydantic v2, sqlalchemy core, ruff, mypy, pytest)|V25
T4|x|M0 policy files DATA_SOURCES.md DATA_POLICY.md TAKEDOWN_POLICY.md PRIVACY.md NOTICE SECURITY.md + README unofficial disclaimer + LICENSE Apache-2.0|V16,V27
T5|x|M0 CLAUDE.md + AGENTS.md agent guardrails|-
T6|x|M0 ADRs: dual-transport-1-core, immutable-promotion, no-query-net, code-only-dist, registry-takedown, oauth-remote|V1,V3,V4,V9,V14
T7|x|M0 CI lint+type+test matrix Windows+macOS+Linux|-
T8|x|M0 config.py + config.example.toml loader; refuse non-loopback remote w/o HTTPS+OAuth|V9
T9|x|M0 machine source registry config/data_sources.toml + sources/registry.py loader|V27
T10|x|M0 local snapshot adapter sources/base.py + local_snapshot.py|V1
T11|x|M0 field allowlist importers/field_policy.py + manifest/checksum importers/manifest.py|V17,V18
T12|x|M0 minimal migrations: schema_migrations data_sources source_snapshots record_provenance source_policy_events|V17
T13|x|M0 enemy parser importers/enemies.py → enemies + enemy_levels|V18
T14|x|M0 stage + level/map/wave/spawn parser importers/stages.py+levels.py for pinned 4-4|V18
T15|x|M0 minimal 4-4 fixture tests/fixtures/stage_4_4 (only test-required fields; no full dump)|V16
T16|x|M0 one deterministic threat rule + evidence analyzers/rules + analyzers/stage.py|V6,V26
T17|x|M0 internal analyze_stage service services/stages.py|V6,V14
T18|x|M0 accept test: 4-4 → stage + enemy occurrence + provenance + threat finding; no wiki text|V5,V6,V16
T19|x|M1 full migrations operator+enemy+stage+analysis tables per PRD §12|-
T20|x|M1 read-only db/connection.py + db/repositories parameterized queries|V2
T21|x|M1 CLI sync + arknights_assets.py adapter (allowlist, HTTPS, size/depth/count/redirect limits)|V1,I.cmd
T22|x|M1 CLI import (local snapshot)|V1,I.cmd
T23|x|M1 CLI validate (integrity_check, foreign_key_check, critical-table, orphan, FTS smoke, golden)|V4,I.cmd
T24|x|M1 versioned data/builds/*.sqlite + atomic current.json promote; retain N prev; unchanged→no-op|V3,V4
T25|x|M1 CLI status + doctor|I.cmd
T26|x|M1 source list/enable/disable/purge --rebuild + source_policy_events|V20,V28,I.cmd
T27|x|M1 get_data_status + get_data_sources services|V27
T28|.|M1 takedown drill test: disable+purge+rebuild, current DB stays until validate|V20
T29|.|M2 mcp/tool_registry.py + envelopes.py (schema_version, status, provenance, read-only hints)|V21,V22,V23
T30|.|M2 bounded Pydantic input/output models models/|V22
T31|.|M2 FTS5 index (names, aliases, stage_code, game_id, tags) + search service|-
T32|.|M2 search_entities tool|V19,V23,I.tool
T33|.|M2 search_stages tool (exact stage_code rank first)|V19,I.tool
T34|.|M2 get_stage tool (include_map/routes/spawns flags + pagination)|V19,V22,I.tool
T35|.|M2 get_enemy tool|V5,V23,I.tool
T36|.|M2 instructions.py (facts/observations/recommendations distinction in first 512 chars)|V6,V7
T37|.|M2 optional MCP resources arknights:// + mcp/resources.py|V27,I.resource
T38|.|M2 local MCP Inspector contract tests (valid/ambiguous/not_found/invalid)|V14,V23
T39|.|M3 rule engine: aerial, block-bypass, def/res skew, ranged-arts, support-aura, pressure-spike, lane/route, tiles/deploy, crowd-control|V6,V7,V26
T40|.|M3 analyze_stage tool (depth summary/standard/detailed)|V6,V7,I.tool
T41|.|M3 golden suite: 4-4 + drones + ranged-arts + multi-route/tiles + operator + CN-only region sep|V5,V6
T42|.|M4 operator+skill+talent importer (character_table, skill_table, aliases)|V18
T43|.|M4 module importer (uniequip_table, uniequip_data, battle_equip_table)|V18
T44|.|M4 get_operator tool (include summary/phases/skills/talents/modules/provenance)|V5,I.tool
T45|.|M4 compare_operator_modules tool (levels 1/2/3, modes facts_only + with_observations)|V7,I.tool
T46|.|M4 module analyzer deterministic observations analyzers/module.py|V6,V7,V26
T47|.|M5 packaging + fresh-clone install smoke (locked deps, uv run serve --transport stdio)|-
T48|.|M5 Claude Code + Codex setup docs (current official formats)|-
T49|.|M5 release audit: no bundled data + policy files complete|V16
T50|.|M5 perf benchmark (lookup p95 <200ms, stage analysis p95 <500ms, startup <2s)|-
T51|.|M6 Streamable HTTP transport transports/streamable_http.py reuse tool_registry+services|V14,I.api
T52|.|M6 OAuth/OIDC resource-server validation auth/oidc.py+principal.py+scopes.py|V9,V10
T53|.|M6 principal/session isolation (no cross-user cache leak)|V14
T54|.|M6 middleware rate_limit + request_limits + redacted logging|V11,V12
T55|.|M6 deploy examples systemd + nginx HTTPS proxy + docker|V9
T56|.|M6 remote validation MCP Inspector + Claude connector + OpenAI API; ChatGPT web if workspace supports|V14
T57|.|M6 remote security/privacy tests (token missing/expired/wrong-issuer/wrong-aud/insufficient-scope, isolation, log scan)|V10,V11,V12
T58|.|M7 threat-model review|-
T59|.|M7 dependency + project-code license audit|-
T60|.|M7 source-registry review + simulated takedown/purge drill|V20,V27
T61|.|M7 local↔remote result parity tests|V14
T62|.|M7 privacy log scan (no token/prompt/args/body)|V12
T63|.|M7 no-bulk-reconstruction test|V19
T64|.|M7 security/policy suite (path traversal, oversized/nested JSON, SQL injection, control chars, prompt injection)|V2,V18,V19
T65|.|M7 tag private-alpha v0.1.0 release|-
T66|.|M1 fix B6: `importers/normalization.py` raw→normalized transform for real `arknights_assets_gamedata` schema (enemy_database id-keyed list + `m_value` attrs + `motion`; `levelId` Title-case → lowercase + `gamedata/levels/` prefix + `.json`; tiles grid-index → x/y; wave action `key` → enemy ref via level `enemies`/`enemyDbRefs`)|V29,V30,V18
T67|.|M1 real-shape contract test: fixture built from real enemy_database/stage_table/level shapes (⊥ synthetic-only); assert `sync`/`import` 4-4 → non-empty enemies+tiles+spawns+`stage_enemies`|V29,V30

id|date|cause|fix
B1|2026-07-17|V5: `sync` reused 1 region-agnostic `base_url` ∀ server → en+cn fetch identical bytes labeled diff region; validation passes on mislabeled data|per-region `base_url_for(server)` (`{server}` token / `base_urls` map) + `_cmd_sync` guard refuses if 2 servers resolve same URL
B2|2026-07-17|V1/PRD17.4: `max_total_download_mb` loaded but never wired → adapter always used hardcoded 512 MiB; operator cap dead|`_download_limits(config)` builds `DownloadLimits(max_total_bytes=mb*1024*1024)`, passed to adapter
B3|2026-07-17|V1/PRD17.4: redirect handler checked HTTPS+count only, no same-domain → 302 to foreign host followed, domain allowlist bypassed|`_BoundedRedirectHandler` default-deny cross-domain vs original host; `allow_cross_domain` opt-out
B4|2026-07-17|V1/PRD17.4: `_total_bytes` per-adapter + fresh adapter per server → `sync --server all` allowed ~2x total cap|shared run-level `DownloadBudget` injected into all adapters in the run
B5|2026-07-17|`json.loads` ran before depth cap → deep-nested JSON raised uncaught `RecursionError` traceback not graceful reject|catch `RecursionError` in `_download` → `SourceAdapterError("JSON exceeds safe nesting depth")`
B6|2026-07-17|H1 verified vs upstream `master`@`413a81a3ff3e`: T13/T14/T21 parsers+fixture `tests/fixtures/stage_4_4` target synthetic shape ≠ real `arknights_assets_gamedata`. (a) real `enemy_database.json` = top-level id-keyed dict of list `{level, enemyData.attributes.<stat>.m_value}`, no `"enemies"` wrapper → `parse_enemies` guard `"enemies" not in database_raw` raises `ImporterError` (in `_HANDLED_ERRORS` → graceful exit 1) at once; stat keys differ (`maxHp`≠`hp`, `magicResistance`≠`res`, `baseAttackTime`≠`attackInterval`). (b) real `levelId`=`Obt/Main/level_main_04-04` (Title-case, no `gamedata/levels/` prefix, no `.json`) → `_discover_level_paths` collects 0/3264 → 0 level files. (c) real level `mapData` no width/height (grid `map`), tiles no x/y → 0/117 kept; wave action enemy under `key` ≠ `enemyId` → 0/26 spawns. (d) `motionType` absent from handbook (real motion @ `enemy_database` `enemyData.motion.m_value`) → aerial rule V6 substrate empty. ∴ real `sync --server en` ⊥ import combat data (halts at enemy-DB guard; if bypassed → silent empty stages). NOT H1's predicted raw `AttributeError`|V29,V30 ∴ T66 normalization transform + T67 real-shape contract test; T13/T14/T21 ⊥ done vs real data (fixture-only)
