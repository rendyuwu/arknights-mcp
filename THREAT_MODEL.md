# Threat Model — Arknights Intelligence MCP (v0.1, private alpha)

Last reviewed: 2026-07-19 · Scope: `v0.1.x` · Review cadence: per release +
on any change to a trust boundary (auth, transports, importer, source registry).

This document is the M7 threat-model review (SPEC §T58). It records the system's
assets, trust boundaries, and the threats crossing them, and maps each threat to
the control that mitigates it — an invariant in [`SPEC.md`](SPEC.md) §V and/or a
requirement in [PRD](Arknights_MCP_PRD_v0.1_FINAL.md) §17 — plus the test that
holds the control in place. It is a design-of-record review, not a penetration
test report; the adversarial test suites it references (T57, T61–T64) are the
executable half of the same effort.

The guiding posture is **fail closed**: on ambiguity, misconfiguration, or
partial failure the system withholds rather than emits, refuses to promote
rather than serve stale/mislabeled data, and rejects rather than degrades.

## 1. System overview

One shared read-only core exposed through two transports (SPEC §G, §V14):

- **Local `stdio`** — MCP protocol on stdout, logs on stderr, no listening port,
  no application auth (the OS user boundary is the trust boundary).
- **Private remote Streamable HTTP** — `POST /mcp` over HTTPS, OAuth/OIDC bearer
  required, per-principal limits, intended to sit behind a maintained reverse
  proxy.

Data is acquired **out of band** by an admin-only CLI (`sync`/`import`), which is
the *only* component permitted to touch the network. The CLI builds an immutable,
versioned SQLite file and promotes it atomically via `data/current.json`. MCP
tools open that file read-only and never reach upstream.

## 2. Assets

| Asset | Why it matters | Primary threat |
|---|---|---|
| Built SQLite database + `current.json` manifest | The served intelligence; integrity gates all responses | Tampering, corruption, stale/mislabeled promotion |
| Per-record provenance chain (`snapshot_id`, `source_path`, `transform_version`, `record_hash`) | Attribution + tamper evidence + takedown scoping | Silent loss → un-attributable / un-purgeable data |
| Remote principal identity (OIDC bearer, `iss\|sub`) | The only gate on remote access | Spoofing, forgery, replay, cross-principal leakage |
| OAuth/OIDC secrets (issuer, audience, jwks_url, scopes) | Compromise = unauthorized access | Disclosure via logs/config/errors |
| Operator host (filesystem, shell, network egress) | Blast radius if the data plane is escaped | Path traversal, SQL/shell injection, SSRF |
| Upstream source repositories | Trust root for imported facts | Data poisoning, schema drift, disappearance |
| Source registry (licenses, permission posture, attribution) | Legal/takedown correctness | Stale attribution, un-executable takedown |

## 3. Trust boundaries

1. **Upstream source ↔ CLI importer.** Everything past this line is untrusted
   data. Crossing is one-way and out of band (CLI only).
2. **Admin CLI ↔ read-only MCP core.** Admin ops (`sync`, `import`, `validate`,
   `purge`, source management) live entirely on the CLI side and are never
   reachable as MCP tools.
3. **Candidate build ↔ promoted (active) database.** A candidate is only
   promoted after the full validation gate passes; the active DB is never
   mutated in place.
4. **Local stdio (OS-user trust) ↔ Remote HTTP (OAuth-principal trust).** The
   remote boundary requires positive authentication; loopback dev is the only
   authless exception, and even that is overridden by `behind_proxy=true`.
5. **Unauthenticated internet ↔ authenticated principal.** Enforced at the
   remote transport independent of bind address.

## 4. Threats and mitigations

Categories follow STRIDE, adapted to this system's actual surface. Each row cites
the enforcing invariant/PRD clause and the test that guards it.

### 4.1 Spoofing / authentication (remote principal boundary)

| # | Threat | Mitigation | Cite | Test |
|---|---|---|---|---|
| S1 | Unauthenticated caller reaches tools on a public endpoint | Non-loopback remote without HTTPS + valid OIDC fails startup; authless non-loopback prohibited | §V9, PRD §17.3 | `test_config`, `test_streamable_http_app`, `test_remote_security_privacy` |
| S2 | Loopback bind used to smuggle a public endpoint past the auth gate (reverse proxy / tunnel) | `[mcp.remote] behind_proxy=true` forces the §V9 gate + bearer enforcement even on `127.0.0.1` | §V40 (B31) | `test_config`, `test_streamable_http_app` |
| S3 | Forged / `alg=none` / symmetric-key-confusion token accepted | RS256 only, `algorithms=["RS256"]`, JWKS signature required, no HS*/`none` | §V10 | `test_oidc_validation`, `test_bearer_auth_asgi` |
| S4 | Token from wrong issuer/audience, or expired, accepted | Validate `iss` (exact, incl. trailing slash) + `aud` (str or array) + `exp` + `iat`, require all, `leeway=60s` | §V10 | `test_oidc_validation`, `test_remote_security_privacy` |
| S5 | Caller with a valid token but without the required scope reaches a tool | Required-scope AND check over `scope` ∪ `permissions`; absent/insufficient → typed `insufficient_scope` reject | §V10 | `test_scopes`, `test_oidc_validation`, `test_remote_security_privacy` |
| S6 | Credential storage becomes an attack target | No username/password storage; OIDC only; secrets via env, never TOML | §V10, PRD §17.3 | `test_config` |

### 4.2 Tampering / injection

| # | Threat | Mitigation | Cite | Test |
|---|---|---|---|---|
| T1 | Arbitrary SQL / shell / filesystem via a tool | Read-only SQLite handle in every MCP process; parameterized SQL only; no such tool exposed | §V2, PRD §17.1 | `test_sqlite_guard`, `test_db_connection` |
| T2 | Prompt injection: imported prose steers the model / selects tools | Field allowlist; imported strings treated as data, never concatenated into instructions/tool descriptions; control chars stripped; length capped | §V18, §V31, PRD §17.6 | `test_field_policy`, `test_text`, `test_import_*` |
| T3 | Nested/unallowlisted JSON leaves smuggle control/bidi chars past the cap | Allowlist + sanitize applied recursively to every nested string leaf; no raw dict/list stored; size capped before encode | §V31 (B8, B9) | `test_field_policy` |
| T4 | Crafted `levelId` escapes the levels tree (path traversal to excel/other files) | `normalize_level_id` forces `gamedata/levels/` prefix; discovery confined to that tree, `.`/`..` and nested `gamedata`/`excel` segments rejected | §V36 (B17) | `test_normalization`, `test_import_stage_4_4` |
| T5 | Active database mutated / replaced under a live handle | Immutable versioned builds; atomic promotion via `current.json`; never mutate active DB in place | §V4, PRD §17.1 | `test_promotion` |
| T6 | Malformed source (dup PK, deep JSON, oversized) crashes/corrupts a multi-region build | Typed `ImporterError` on constraint anomalies; JSON depth + size + record-count caps; fail closed, no partial promote | §V33, §V30, PRD §17.4 | `test_import_enemies`, `test_normalization`, `test_arknights_assets_adapter` |
| T7 | Sync follows a redirect to a foreign host / interpolates remote values into a shell | Domain allowlist, HTTPS default, same-domain redirect policy, no shell interpolation of remote values | §V1, PRD §17.4 (B1–B5) | `test_arknights_assets_adapter`, `test_cli_sync` |

### 4.3 Information disclosure

| # | Threat | Mitigation | Cite | Test |
|---|---|---|---|---|
| I1 | Query surface used to reconstruct the dataset (de facto redistribution) | No bulk dump / DB download / unbounded pagination / enumeration; search limit default 10 / max 50, page_size max 100; out-of-range rejected at model **and** service | §V19, PRD §17.1 (B23) | `test_input_models`, `test_search_service`, `test_search_entities_tool` |
| I2 | Oversized response leaks bulk data / bloats context | Default response `< 200 KB`, measured worst-case ASCII-escaped (fail-closed over-measure); large sections behind include flags / pagination | §V22 (B21) | `test_envelopes`, `test_get_stage_tool` |
| I3 | Bearer token / prompt / args / body / secrets land in logs | Default logs exclude full prompt, full args, response body, auth header, bearer token, raw source record, roster/account | §V12, PRD §17.5 | `test_redacted_logging_middleware`, `test_remote_security_privacy` |
| I4 | Error responses leak stack traces / local paths | Typed status envelope; errors never carry stack trace or local path | §V23 | `test_envelopes`, `test_mcp_inspector_contract` |
| I5 | `get_data_sources` leaks secrets / fs paths / OAuth config / takedown notes | Single public-safe `registry.public_view()` projection shared by CLI + tool; internal-only fields dropped | §V27, §V34 (B15, B18) | `test_source_registry`, `test_cli_source` |
| I6 | Cross-principal cache/session leakage on remote | Principal-bound sessions; no cross-user cache | §V14, PRD §17.3 | `test_session_isolation`, `test_remote_security_privacy` |
| I7 | Region confusion: cn queried against en-only build reported as fresh `ok` | Region-scoped verdict; region with no snapshot → `data_stale` + region warning; en/cn never silently mixed | §V5 (B24) | `test_mcp_resources`, `test_data_status_service` |

### 4.4 Denial of service / resource exhaustion

| # | Threat | Mitigation | Cite | Test |
|---|---|---|---|---|
| D1 | Remote caller exhausts CPU/memory/connections | Per-principal rate limit + concurrency limit + request timeout + request cap + response cap | §V11, PRD §17.3 | `test_rate_limit_middleware`, `test_request_limits_middleware`, `test_remote_middleware_stack` |
| D2 | Deeply nested / huge source JSON exhausts the importer | Depth cap (graceful reject, not `RecursionError`), size + record-count caps | §V30, PRD §17.4 (B5) | `test_arknights_assets_adapter`, `test_normalization` |

### 4.5 Elevation of privilege / admin surface

| # | Threat | Mitigation | Cite | Test |
|---|---|---|---|---|
| E1 | Admin op (sync/import/validate/purge/source mgmt) invoked as an MCP tool | Admin ops are CLI-only, never exposed in the tool registry | §V28, PRD §17.1 | `test_tool_registry`, `test_mcp_inspector_contract` |
| E2 | Tool reaches upstream network at query time (SSRF-style pivot) | Runtime MCP tool makes no outbound source request; only CLI sync/import touch allowlisted sources | §V1, PRD §17.1 | `test_sqlite_guard`, `test_serve_transport` |

### 4.6 Supply chain / data integrity

| # | Threat | Mitigation | Cite | Test |
|---|---|---|---|---|
| P1 | Poisoned / drifted upstream schema silently produces wrong or empty data | Fail-closed promotion (validate gate: integrity_check, foreign_key_check, critical-table, row-count, golden, FTS integrity); non-empty source yielding 0 rows fails closed | §V3, §V4, §V30 (B6, B20) | `test_validate`, `test_promotion`, `test_real_shape_contract` |
| P2 | Release artifact ships raw data / prebuilt DB / game content | Release audit allowlists code + metadata only; no snapshot, DB, art, audio, prose | §V16 | `test_release_audit`, `test_policy_files` |
| P3 | Taken-down source keeps surfacing after purge | `purge --rebuild` cascade-deletes attributable rows across all domains, rebuilds FTS from survivors, flips registry `enabled=false`, journals only after promote | §V32 (B10, B12, B19) | `test_takedown_drill`, `test_cli_source` |
| P4 | Un-attributable / un-purgeable records (no provenance) | Every imported record carries `snapshot_id` + source key + `transform_version` + `record_hash`; level-derived rows stamped too | §V17 (B7) | `test_manifest`, `test_repositories_stages` |
| P5 | Game credentials requested/stored/transmitted | Never request, store, or transmit Arknights credentials | §V15, PRD §17.5 | `test_policy_files` (PRIVACY.md), `test_config` |

## 5. Residual risks

These are **accepted** for v0.1 private alpha and re-evaluated before any public
posture (§17.7):

- **Operator host compromise.** If the machine running the server is
  compromised, the read-only data plane and CLI/MCP boundary do not protect the
  attacker's own host. Mitigation is operational (least-privilege FS perms,
  maintained reverse proxy), not in scope for this codebase.
- **Upstream repository compromise at the pinned commit.** The importer trusts
  the pinned source commit's *contents*; a compromised-but-well-formed upstream
  could inject plausible false facts. Detection relies on the CN cross-validator
  (T69) and golden tests, not cryptographic provenance of upstream.
- **Reverse proxy / TLS misconfiguration.** HTTPS termination, body limits, and
  access-log redaction at the proxy are the operator's responsibility;
  `behind_proxy=true` forces app-side enforcement but cannot audit the proxy.
- **Confused-deputy via a trusted client.** A compromised MCP client with a
  valid principal is bounded by scopes + rate limits but can issue any permitted
  read within those limits.
- **Timing / volumetric inference.** Rate + result caps bound bulk
  reconstruction (§V19) but do not eliminate slow, distributed scraping by a
  legitimately authenticated principal.

## 6. Explicitly out of scope (v0.1)

Per SPEC §C and PRD §17.7, the following are **not** addressed here and gate a
separate public-readiness review, not a single flag:

- Public multi-tenant hosting, tenant isolation beyond per-principal binding.
- Game login, roster storage, squad optimizer, combat sim, gacha planning.
- Community/wiki prose, art, audio, story, voice, full announcement bodies.
- Abuse response, cost controls, and monitoring for a public service.

## 7. How this model stays honest

The controls above are not asserted — they are executed. The adversarial suites
land in the remaining M7 tasks and must pass before the `v0.1.0` tag (§T65):

- **T57** — remote security/privacy (token missing/expired/wrong-issuer/
  wrong-aud/insufficient-scope, isolation, log scan). *Done.*
- **T61** — local↔remote result parity (§V14).
- **T62** — privacy log scan: no token/prompt/args/body (§V12).
- **T63** — no-bulk-reconstruction (§V19).
- **T64** — security/policy suite: path traversal, oversized/nested JSON, SQL
  injection, control chars, prompt injection (§V2, §V18, §V19).

A change that adds a tool, a transport, a source, or touches auth **must** revisit
Section 3 (trust boundaries) and Section 4, and add or update the enforcing test.
