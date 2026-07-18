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
V4: candidate promote only after validate pass: `PRAGMA integrity_check` + `PRAGMA foreign_key_check` + critical-table + row-count + golden + FTS5 `integrity-check`. `PRAGMA integrity_check` ⊥ verify FTS5 shadow tables ∴ run FTS5's own `INSERT INTO entity_fts(entity_fts) VALUES('integrity-check')` (prepared as write → short-lived `mode=rw` handle; no writes → candidate byte-identical). promotion atomic via `current.json`. ⊥ mutate active DB in place.
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
V19: ⊥ bulk dump | DB download | unbounded pagination | enumeration → dataset reconstruction. search limit default 10 max 50; page_size max 100. out-of-range limit *rejected* at BOTH the model (`SearchEntitiesInput` `ge/le`) AND the service (`search_entities` → `ValueError`); ⊥ silent clamp|widen in either (one contract, same enforcement both places).
V20: `disable` → stop new sync, keep current data. `purge --rebuild` → remove only rows attributable to source; current DB active until rebuilt candidate validates.
V21: required tool fields backward-compat within v0.1; additive optional fields preferred; breaking change → `schema_version` bump + ADR.
V22: default tool response `< 200 KB`. large map/spawn → explicit include flag | pagination. cap measured at worst-case ASCII-escaped byte length (`ensure_ascii=True`, ≥ raw UTF-8 ∀ char) ∴ cap holds on wire regardless of transport encoding (compact UTF-8 | `\uXXXX` escapes); fail-closed = over-measure, ⊥ under-measure.
V23: ∀ tool result → typed status ∈ {ok,partial,not_found,ambiguous,unsupported_server,data_stale,database_unavailable,schema_incompatible,analysis_unavailable,internal_error}. error ⊥ stack trace | local path.
V24: absent entity → typed not_found | region_unavailable | data_stale + suggested admin action. ⊥ query-time download/scrape fallback.
V25: `mcp>=1.28.1,<2`; exact resolved version in `uv.lock`. SDK v2 migration → ADR.
V26: analyzer rules on typed fields (⊥ NL keyword match). missing field → reduce confidence | limitation. conflicting source fields → omit conclusion + warn.
V27: source registry complete ∀ enabled source: `source_id` + owner + URL + purpose/domains + regions + license/permission status + attribution + enabled + `last_reviewed` + snapshot commit. `get_data_sources` ⊥ secrets | local fs path | OAuth config | takedown correspondence.
V28: admin ops (sync, import, validate, purge, source mgmt) CLI-only. ⊥ exposed as MCP tool.
V29: importer parse contract verified vs real `arknights_assets_gamedata` schema; ⊥ validate data path solely on synthetic fixture matching parser. real shapes: `enemy_database.json` top-level id-keyed dict → list `{level, enemyData.attributes.<stat>.m_value}` (no `"enemies"` wrapper; `maxHp`/`magicResistance`/`baseAttackTime` ≠ `hp`/`res`/`attackInterval`); `stage_table.levelId` = `Obt/Main/level_main_04-04` (Title-case, no `gamedata/levels/` prefix, no `.json`); level tiles grid-indexed (no `x`/`y`; `passableMask` ≠ `passable`), wave action enemy under `key` ≠ `enemyId`; enemy motion @ `enemy_database` `enemyData.motion.m_value` (handbook no `motionType`). field maps VERIFIED vs LIVE upstream @`413a81a3ff3e` (T68, ⊥ "inferred"): `maxHp`→hp, `magicResistance`→res, `baseAttackTime`→attackInterval, `massLevel`→weight, `lifePointReduce`→lifePointReduction, `motion`→motion_type, `preDelay`→spawnTime, `maxTimeWaitingForNextWave`→maxTimeWaiting, positional route/wave index; real 4-4 → non-null hp/res/attackInterval/weight/lifePointReduction/motion + non-empty tiles/spawns/`stage_enemies`.
V30: raw upstream shape ≠ normalized tables → explicit `importers/normalization.py` transform ! bridge. sync/import ! report per-stage import counts; non-empty source yielding 0 levels/tiles/spawns/`stage_enemies` → fail closed (⊥ silent empty build).
V31: field allowlist + sanitize applied recursively ∀ nested string leaf in kept JSON (dict|list) value + stored source fragment; ⊥ store raw unallowlisted dict|list; cap serialized size before JSON-encode.
V32: `purge --rebuild` cascade-deletes ∀ table tracing to purged source (children→parents, FK on): stage/enemy/zone + operator/skill/module/talent domains. residual FK from a non-purged source → `PurgeError` (fail-closed; ⊥ uncaught `IntegrityError`; ⊥ strip a non-purged stage's occurrences). purge ! flip machine-registry `enabled=false` (takedown = disable+purge). purge policy-event journaled durably only after candidate validates+promotes (⊥ phantom event on failed rebuild). purge ! rebuild `entity_fts` (standalone FTS5, no triggers) from surviving rows ∴ ⊥ stale search doc for a purged source (else taken-down entity keeps surfacing via name/alias/game_id, V16).
V33: importer maps source constraint anomaly (dup PK | variant | index) → typed `ImporterError`; ⊥ uncaught sqlite `IntegrityError` tearing down multi-region build.
V34: 1 public-safe source projection fn shared by `source list` + `get_data_sources`; ⊥ divergent field allowlists across the 2 surfaces. both surfaces ! route the SAME projection (`registry.public_view`); ⊥ 2nd independent field enumeration (e.g. service `SourceInfo`) re-forking the allowlist. regression test ! assert set-equality of emitted keys minus DB-only enrichment (`active_snapshots`); `policy_notes`-disjoint + N named-field checks = weak, misses re-fork (B18).
V35: analyzer summary count = distinct `evidence.ref`, ⊥ raw occurrence rows → 1 entity across ≥2 level variants counts once.
V36: stage `levelId` → level-file discovery confined to levels tree: normalized path ! startswith `gamedata/levels/` + endswith `.json`; ⊥ `.`|`..` segment; ⊥ nested `gamedata`|`excel` segment. `normalize_level_id` ! force `gamedata/levels/` prefix ∴ crafted `levelId` ⊥ fetch excel table | escape tree (L8 post-T66-normalize).
V37: DRY. shared logic exactly 1 home. ⊥ copy-paste fn|SQL|guard|const across modules. N≥2 identical|near-identical copies → extract to shared util|importer helper. behavioral variant ! explicit param|arg — ⊥ silent divergent copies (e.g. `_as_str` sanitize in 1 module ≠ raw in another). extends V14 (cross-transport dedup) → intra-codebase.
V38: py source module ≤ 500 lines target, ⊥ > 800 hard cap (excl. generated lockfile). module accreting ≥3 distinct responsibility groups (e.g. CLI command groups `sync`|`import`|`validate`|`status`/`doctor`|`source`) → split to package: 1 module/group + shared `_shared` helpers. ⊥ 1 file mixing parse+DB+HTTP concerns (per §C separated layers).
V39: spawn-time analytics only on a comparable time base. importer `spawn_time` = fragment-relative `preDelay` (upstream `spawnTime` null → `normalization.py:372` fallback), ⊥ stage-absolute; wave+fragment `preDelay` offsets dropped (`_normalize_waves` keeps fragment `actions` only) ∴ `stage_enemies.first/last_spawn_time` = min/max preDelay across ALL waves ≠ elapsed window. rule ⊥ conclude burst|window from cross-wave first/last min/max (extends §V26 typed-field-only). ∴ pick: (a) persist wave+fragment offset → absolute base pre-aggregate, | (b) confine window to 1 wave+fragment, | (c) downgrade confidence + record limitation "spawn window fragment-relative, aggregated across waves; may overstate burst".

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
T28|x|M1 takedown drill test: disable+purge+rebuild, current DB stays until validate|V20
T29|x|M2 mcp/tool_registry.py + envelopes.py (schema_version, status, provenance, read-only hints)|V21,V22,V23
T30|x|M2 bounded Pydantic input/output models models/|V22
T31|x|M2 FTS5 index (names, aliases, stage_code, game_id, tags) + search service|-
T32|x|M2 search_entities tool|V19,V23,I.tool
T33|x|M2 search_stages tool (exact stage_code rank first)|V19,I.tool
T34|x|M2 get_stage tool (include_map/routes/spawns flags + pagination)|V19,V22,I.tool
T35|x|M2 get_enemy tool|V5,V23,I.tool
T36|x|M2 instructions.py (facts/observations/recommendations distinction in first 512 chars)|V6,V7
T37|x|M2 optional MCP resources arknights:// + mcp/resources.py|V27,I.resource
T38|x|M2 local MCP Inspector contract tests (valid/ambiguous/not_found/invalid)|V14,V23
T39|x|M3 rule engine: aerial, block-bypass, def/res skew, ranged-arts, support-aura, pressure-spike, lane/route, tiles/deploy, crowd-control|V6,V7,V26
T40|x|M3 analyze_stage tool (depth summary/standard/detailed)|V6,V7,I.tool
T41|x|M3 golden suite: 4-4 + drones + ranged-arts + multi-route/tiles + operator + CN-only region sep|V5,V6
T42|x|M4 operator+skill+talent importer (character_table, skill_table, aliases)|V18
T43|x|M4 module importer (uniequip_table, uniequip_data, battle_equip_table)|V18
T44|x|M4 get_operator tool (include summary/phases/skills/talents/modules/provenance) + operator golden extends T41 suite (deferred from T41: M3 had no operator read path — `services/operators.py` stub, importer T42 + this tool unbuilt ∴ operator facts+region+provenance scenario → `tests/golden` lands here w/ get_operator)|V5,I.tool
T45|x|M4 compare_operator_modules tool (levels 1/2/3, modes facts_only + with_observations)|V7,I.tool
T46|x|M4 module analyzer deterministic observations analyzers/module.py|V6,V7,V26
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
T66|x|M1 fix B6: `importers/normalization.py` raw→normalized transform for real `arknights_assets_gamedata` schema (enemy_database id-keyed list + `m_value` attrs + `motion`; `levelId` Title-case → lowercase + `gamedata/levels/` prefix + `.json`; tiles grid-index → x/y; wave action `key` → enemy ref via level `enemies`/`enemyDbRefs`)|V29,V30,V18,V36
T67|x|M1 real-shape contract test: fixture built from real enemy_database/stage_table/level shapes (⊥ synthetic-only); assert `sync`/`import` 4-4 → non-empty enemies+tiles+spawns+`stage_enemies`|V29,V30
T68|x|M1 CI-only real-shape validation vs LIVE upstream (T67 is fixture-only ∴ proves internal consistency, ⊥ real-upstream fidelity of inferred mappings): fetch pinned `arknights_assets_gamedata` (+ `kengxxiao_gamedata` CN validator) commit, run `sync`/`import`, assert real 4-4 → non-null `hp`/`res`/`attackInterval`/`weight`/`lifePointReduction`/`motion` + non-empty tiles/spawns/`stage_enemies`; ⊥ commit fetched raw data (V16 code-only, fetch→discard). on pass → promote inferred mappings (`massLevel`→weight, `lifePointReduce`→lifePointReduction, `preDelay`→spawnTime, `maxTimeWaitingForNextWave`→maxTimeWaiting, positional route/wave index) into §V29 + drop `normalization.py` "inferred" caveat|V16,V29,V30,C
T69|x|M1 CI-only `kengxxiao_gamedata` CN cross-validator (deferred from T68): kengxxiao CN `enemy_database.json` = `{"enemies":[{"Key":<id>,"Value":[levels]}]}` (list, ⊥ id-keyed dict of `arknights_assets`) ∴ needs own normalization bridge before pipeline import. fetch pinned kengxxiao CN commit `6b6ac60f` + primary CN, cross-check shared enemy stats (`maxHp`/`baseAttackTime`/`massLevel`/`motion`) agree EN-source-CN vs kengxxiao-CN; ⊥ runtime dep; ⊥ override primary; ⊥ commit fetched raw data|V29,V30,C
T70|x|DRY: extract dup coerce helpers `_as_int`/`_as_float`/`_as_str`/`_json_or_none` (3 copies: `importers/enemies.py:66-81` + `levels.py:87-100` + `stages.py:64-69`) → 1 shared home (`util/coerce.py`); resolve `_as_str` divergence (levels `sanitize_text` vs enemies/stages raw) via explicit `sanitize=` param, ⊥ silent variant copies|V37
T71|x|DRY: unify `_is_placeholder` (2 copies: `cli.py:114` `(str)` + `config.py:41` `(str\|None)`) → 1 shared fn (`str\|None` signature)|V37
T72|x|DRY: dedup `INSERT INTO record_provenance` (`enemies.py:167` inline vs `stages.py:121` `_insert_provenance` helper) → shared helper in `importers/manifest.py`; both importers route through it|V37,V17
T73|x|DRY: extract repeated `sqlite3.IntegrityError`→`ImporterError` guard (3 sites: `enemies.py:225` + `levels.py:226` + `db/purge.py`) → 1 shared guard (ctx mgr\|decorator)|V37,V33
T74|x|fix B18: `get_data_sources`/service `SourceInfo` route through single `registry.public_view()` projection ∴ `source list --json` + MCP tool emit identical field set (minus DB-only `active_snapshots`); strengthen `test_public_projections_do_not_diverge` → assert `cli_keys == svc_keys - {active_snapshots}`, ⊥ weak named-field check|V34,V27
T75|x|V38: split `cli.py` (604 ln, ≥3 cmd groups) → `cli/` package: 1 module/command-group (`sync`\|`import`\|`validate`\|`status`+`doctor`\|`source`) + shared `cli/_shared.py` helpers; pure structural move, ⊥ behavior change|V38
T76|x|fix B30: `analyzers/rules/pressure_spike.py` ⊥ conclude burst from cross-wave `first/last_spawn_time` min/max (fragment-relative preDelay, ≠ elapsed) → per V39 pick (c) downgrade conf + record limitation, \| (a)/(b) expose wave+fragment offset to `StageThreatContext` for real window; add `test_analyzer_rules.py` case: 1 enemy across ≥6 fragments @low `preDelay` ⊥ report spike (\| reports w/ limitation + reduced conf)|V39,V26

id|date|cause|fix
B1|2026-07-17|V5: `sync` reused 1 region-agnostic `base_url` ∀ server → en+cn fetch identical bytes labeled diff region; validation passes on mislabeled data|per-region `base_url_for(server)` (`{server}` token / `base_urls` map) + `_cmd_sync` guard refuses if 2 servers resolve same URL
B2|2026-07-17|V1/PRD17.4: `max_total_download_mb` loaded but never wired → adapter always used hardcoded 512 MiB; operator cap dead|`_download_limits(config)` builds `DownloadLimits(max_total_bytes=mb*1024*1024)`, passed to adapter
B3|2026-07-17|V1/PRD17.4: redirect handler checked HTTPS+count only, no same-domain → 302 to foreign host followed, domain allowlist bypassed|`_BoundedRedirectHandler` default-deny cross-domain vs original host; `allow_cross_domain` opt-out
B4|2026-07-17|V1/PRD17.4: `_total_bytes` per-adapter + fresh adapter per server → `sync --server all` allowed ~2x total cap|shared run-level `DownloadBudget` injected into all adapters in the run
B5|2026-07-17|`json.loads` ran before depth cap → deep-nested JSON raised uncaught `RecursionError` traceback not graceful reject|catch `RecursionError` in `_download` → `SourceAdapterError("JSON exceeds safe nesting depth")`
B6|2026-07-17|H1 verified vs upstream `master`@`413a81a3ff3e`: T13/T14/T21 parsers+fixture `tests/fixtures/stage_4_4` target synthetic shape ≠ real `arknights_assets_gamedata`. (a) real `enemy_database.json` = top-level id-keyed dict of list `{level, enemyData.attributes.<stat>.m_value}`, no `"enemies"` wrapper → `parse_enemies` guard `"enemies" not in database_raw` raises `ImporterError` (in `_HANDLED_ERRORS` → graceful exit 1) at once; stat keys differ (`maxHp`≠`hp`, `magicResistance`≠`res`, `baseAttackTime`≠`attackInterval`). (b) real `levelId`=`Obt/Main/level_main_04-04` (Title-case, no `gamedata/levels/` prefix, no `.json`) → `_discover_level_paths` collects 0/3264 → 0 level files. (c) real level `mapData` no width/height (grid `map`), tiles no x/y → 0/117 kept; wave action enemy under `key` ≠ `enemyId` → 0/26 spawns. (d) `motionType` absent from handbook (real motion @ `enemy_database` `enemyData.motion.m_value`) → aerial rule V6 substrate empty. ∴ real `sync --server en` ⊥ import combat data (halts at enemy-DB guard; if bypassed → silent empty stages). NOT H1's predicted raw `AttributeError`|V29,V30 ∴ T66 normalization transform + T67 real-shape contract test; T13/T14/T21 ⊥ done vs real data (fixture-only)
B7|2026-07-17|V17: level-file-derived rows (stage_maps/tiles/routes/waves/spawns/stage_enemies) written by `insert_level` w/o `make_record_provenance`, tables had no `provenance_id` col → threat-analysis substrate carried no per-record provenance/tamper hash (snapshot `manifest_hash` covers bytes, not per-record)|migration `0006_provenance_backfill.sql` adds `provenance_id` FK on level+zone tables; `stages.py` creates a per-level-file provenance row; `insert_level` stamps every row (test_level_rows_carry_provenance, test_zone_carries_provenance)
B8|2026-07-17|V18: `apply_allowlist` sanitized only top-level `str`; kept dict/list values (abilities, immunities, environment, specialProperties, route positions) stored raw as JSON → nested control/bidi chars survived + escaped the length cap, reaching `*_json` cols exposed to the LLM client|`field_policy.sanitize_value()` recurses over nested string leaves, routed by `apply_allowlist`+`levels.py` (test_allowlist_sanitizes_nested_string_leaves) ∴ V31
B9|2026-07-17|V18: `stage_spawns.source_fragment_json` stored the entire raw wave action → any prose/injection field in the source action was JSON-dumped in, contra the known-keys-only fragment contract|`SPAWN_ACTION_ALLOWLIST`; `levels.py` stores the allowlisted fragment only (test_spawn_source_fragment_is_allowlisted) ∴ V31
B10|2026-07-17|V20: `_purge_source_rows` deleted `record_provenance` but cascaded only enemy/stage children; operators/skills/modules declare `provenance_id NOT NULL REFERENCES record_provenance` w/o ON DELETE CASCADE → the delete raised `IntegrityError`, purge aborted, a source w/ operators was never purgeable (latent: M1 operator importers stub)|`db/purge.py` cascades operator/skill/module/talent before `record_provenance`; `IntegrityError`→`PurgeError` fail-closed (test_purge_cascades_operator_domain) ∴ V32
B11|2026-07-17|V20: purge `append_event(purge)` was journaled to `policy_events.jsonl` before `purge_and_rebuild` → a failed-validation rebuild left a durable phantom purge; the next sync `materialize_policy_events` wrote it into the build while the source rows were still present|`_cmd_source_purge` builds the event in memory, journals it durably only after validate+promote (test_source_purge_failed_validation_no_phantom_purge) ∴ V32
B12|2026-07-17|V20: purge flipped `data_sources.enabled=0` only inside the rebuilt build; the machine-registry TOML that gates `_cmd_sync` was untouched → `purge X` w/o a prior `disable X` was repopulated by the next sync; registry vs build enabled-state diverged|`_cmd_source_purge` also `set_source_enabled(id, False)` + journals the disable at once (test_source_purge_also_disables_registry) ∴ V32
B13|2026-07-17|`enemy_levels UNIQUE(enemy_pk, level_variant)` (+ stage_waves/routes/tiles uniqueness): a repeated or absent index in the source (`_as_int(...) or 0` → two missing `level` collide at variant 0) raised an uncaught `IntegrityError` that tore down the whole multi-region candidate build|`enemies.py`+`levels.py` catch `sqlite3.IntegrityError`→`ImporterError` (test_duplicate_level_variant_fails_gracefully) ∴ V33
B14|2026-07-17|V6: aerial rule `_summary(len(evidence))` counted one `EvidenceItem` per (enemy, level_variant); a flyer at 2 variants reported "2 aerial enemy types" for 1 enemy + duplicate evidence refs|`analyzers/rules/aerial.py` counts distinct `evidence.ref` (test_same_flyer_at_two_variants_counts_as_one_type) ∴ V35
B15|2026-07-17|V27: two divergent public projections — `registry.public_view()` dropped `private_hosting_status` via `_INTERNAL_ONLY_FIELDS` but the `get_data_sources` service included it + bypassed `public_view()` → `source list` and the MCP tool emitted different field sets; the allowlist was maintained in 2 places|single `_INTERNAL_ONLY_FIELDS={"policy_notes"}` projection (private_hosting_status intended-public, PRD §13.10); `get_data_sources` routes through it (test_public_projections_do_not_diverge) ∴ V34
B16|2026-07-17|V16/D4: `DEFAULT_MIGRATIONS_DIR = parents[3]/"migrations"` resolved to the repo root, outside the packaged `src/arknights_mcp` tree → a non-editable install found 0 `.sql` files and `build_database`/`_expected_schema_version` broke (unexercised: T47 install-smoke not done)|moved `migrations/*.sql`→`src/arknights_mcp/migrations/`; `db/migrations.py` resolves via `importlib.resources` (ships in wheel); enforcing test deferred to T47
B17|2026-07-18|T66 normalizes `levelId` (real `Obt/Main/level_main_04-04`→`gamedata/levels/obt/main/level_main_04-04.json`) ∴ real level files fetch; but old `_discover_level_paths` L8 gate accepted a levelId only if its *raw* value startswith `gamedata/levels/` → real Title-case ⊥ prefix → collected 0/3264 files (B6b), & post-normalize every levelId maps under `gamedata/levels/` ∴ raw-prefix check ⊥ valid L8 boundary; excel craft `gamedata/excel/character_table.json` + traversal must reject *after* normalize|`_discover_level_paths` confines the *normalized* path to a clean level path (under `gamedata/levels/`+`.json`, ⊥ `.`/`..` seg, ⊥ nested `gamedata`/`excel` seg); `normalize_level_id` always forces the `gamedata/levels/` prefix ∴ V36
B18|2026-07-18|V34/V27 (B15 fix incomplete): `get_data_sources` builds service `SourceInfo` by own field enumeration, ⊥ routes through `registry.public_view()` drop-list → `source list --json` emits `adapter_version`+`transform_version` but MCP tool omits both = 2 divergent public allowlists again; `test_public_projections_do_not_diverge` asserts only `policy_notes`-disjoint + 3 named fields, ⊥ set-equality ∴ blind to the re-fork|route `get_data_sources`/`SourceInfo` through the single `registry.public_view()` projection ∴ both surfaces emit identical keys (minus DB-only `active_snapshots`); strengthen test → set-equality assert; §V34 tightened ∴ T74
B19|2026-07-18|V32/V16/V20: `_purge_source_rows` DELETEs base rows but `entity_fts` is a standalone FTS5 index w/ no triggers (0007) → the purged source's game_id/name/alias docs survive in the index ∴ `search_entities(server=purged)` still returns the taken-down entity + `get_*` 404s on the dangling game_id (V16 leak); `purge_and_rebuild`'s filtered copy left the promoted index stale|`db/purge.py` calls `rebuild_search_index(conn)` (DELETE FROM entity_fts + repopulate from surviving rows, single §V37 home) inside the purge txn; drill asserts en FTS docs gone + en search empty while cn stays live (test_takedown_drill_purges_only_target_keeps_others_live) ∴ V32
B20|2026-07-18|V4: `PRAGMA integrity_check` ⊥ verify FTS5 shadow-table consistency + `_fts_smoke` only ran a no-match MATCH (returns 0, always passes) → a stale/corrupt FTS index passed the promotion gate|`_fts_smoke` runs FTS5's own `INSERT INTO entity_fts(entity_fts) VALUES('integrity-check')`; prepared as a write ∴ short-lived `mode=rw` handle (performs no writes → candidate byte-identical, T24) (test_corrupt_fts_index_detected) ∴ V4
B21|2026-07-18|V22: `serialized_size` measured w/ `ensure_ascii=False` (compact UTF-8) → a CN-heavy envelope ~199KB is ~2x larger once the T51 Streamable-HTTP serializer emits `\uXXXX` escapes (3-byte CJK → 6 ASCII) ∴ envelope passes the cap but exceeds 200KB on the wire|measure w/ `ensure_ascii=True` (JSON default) = worst-case upper bound ≥ any UTF-8 encoding ∴ cap holds on the wire ∀ transport encoding; fail-closed (over-measure, ⊥ under) ∴ V22
B22|2026-07-18|T24/V14: `GROUP_CONCAT(a.alias, ' ')` (enemy+operator alias agg in `search_index.py`) had no ORDER BY; SQLite concat order is arbitrary → two builds of byte-identical source could emit aliases in different order → FTS doc bytes differ → file `database_hash` differs → T24 "unchanged→no-op" treats identical data as changed + byte-reproducibility broken|`GROUP_CONCAT(a.alias, ' ' ORDER BY a.alias)` pins the concat order (SQLite ≥3.44 ordered-aggregate) in `_ENEMY_SQL`/`_OPERATOR_SQL`
B23|2026-07-18|V19: service `_clamp_limit` silently `max(1, min(limit, MAX))` clamped while `SearchEntitiesInput` *rejects* out-of-range (`ge=1, le=50`) → two divergent V19 behaviors; a caller reaching the service directly (or a transport skipping model validation) got a silent widen/narrow ⊥ a rejection|`_validate_limit` raises `ValueError` on out-of-range (mirrors the model) ∴ one V19 contract enforced identically both places (test_out_of_range_limit_rejected) ∴ V19
B24|2026-07-18|V5 (T32-T38 review): `arknights://status/{server}` `_make_status_handler` filtered `snapshots` by region but reused the *global* `DataStatus.status`/`warnings`/`suggested_action` + derived env_status from the global verdict → a region w/ 0 active snapshots (en-only build, query cn) reported `ok` + empty snapshots + no staleness warning ∴ client concludes cn data present+fresh when absent|region-scope the whole verdict: no region snapshot → `data_stale` + region warning/action; override `data.status`/`warnings`/`suggested_action` to the region view (test_status_other_region_has_no_snapshots asserts cn→data_stale) ∴ V5
B25|2026-07-18|V19 (T32-T38 review): `get_stage` single `page`/`page_size` cursor computed one `offset` applied to tiles+routes+spawns together → a multi-section call could not page one large section without shifting the smaller ones off (page 2 for tiles returned `routes:[]`/`spawns:[]` as if none exist); sections coupled to one cursor|per-section cursors `map_page`/`routes_page`/`spawns_page` (each bounded `PageParams`), independent offset + `*_page` descriptor ∴ hold whole stage in one call yet page a large section alone (test_sections_page_independently) ∴ V19
B26|2026-07-18|V19 (T32-T38 review): `_SPAWN_COUNT_SQL` JOINed only `stage_waves` while `_SPAWNS_SQL` also INNER JOIN `enemies` → a spawn w/ an absent enemy row (SQLite ⊥ enforce FK w/o `PRAGMA foreign_keys=ON`) is counted but not returned ∴ `total`>rows, `has_more` stuck true on the last real page → a paging client loops on a never-filling page|`_SPAWN_COUNT_SQL` mirrors the `enemies` join exactly ∴ count = returnable rows ∴ V19
B27|2026-07-18|V22 (T32-T38 review): `get_stage(include_map=True)` on a stage w/ no `stage_maps` row still built an all-null `StageMapFacts` + emitted a `map` object → "map absent" indistinguishable from "map present but empty"|surface the map section only when `raw_map is not None or tile_count>0`, else leave it `None` ∴ the tool emits no `map` key for a genuinely map-less stage ∴ V22
B28|2026-07-18|I.tool (T32-T38 review): `get_stage` nested `tiles`+`tiles_page` inside the `map` object while `routes_page`/`spawns_page` sat at the top level of `data` → asymmetric wire shape, the per-section page descriptors live in 2 different places ∴ a client can't use one uniform `data[section+'_page']` lookup|`tiles` is its own top-level paged section (`tiles`/`tiles_page`) like routes/spawns; `map` is header-only ∴ all 3 heavy sections share one shape (test_include_map_returns_header_and_paged_tiles)
B29|2026-07-18|V37 (T32-T38 review): `services/stages._parse_abilities` re-implemented the try/except `json.loads` decode that T37 centralized in `util.coerce.json_load` → 2 read-side decode homes ∴ a future decode change must be made in both or they silently diverge|`_parse_abilities` decodes through `json_load`, applies only the list/str shaping the analyzer needs on top; dropped the local `import json` ∴ V37
B30|2026-07-19|T39-T46 review (verified vs real 4-4 @`413a81a3ff3e`): `pressure_spike.py:49-62` sets `window = last_spawn_time - first_spawn_time` & flags burst @0.8 conf if `total_count≥6` & `window≤12s`. but upstream `spawnTime` always null → importer falls back to `action.preDelay` (`normalization.py:372`) = fragment-relative offset; wave `preDelay` + fragment `preDelay` dropped by `_normalize_waves`; `stage_enemies.first/last_spawn_time` (`levels.py:312-318`) = min/max of that fragment-relative preDelay across ALL waves ≠ elapsed real time ∴ enemy trickled across ≥6 fragments @low action-`preDelay` collapses window≈0 → false "concentrated burst". real 4-4: `enemy_1012_dcross` window=0 count=4; 2 `nsabr` actions importer sees @spawn_time=3 really @6s(w0)/@23s(w1). NOTE §V29 `preDelay`→`spawnTime` = field-NAME map, ⊥ a time-absolute claim|V39
