# ADR 0013: Retire the extra-locale (ja/ko) NAME-alias axis — EN+CN only

- **Status:** Accepted
- **Date:** 2026-07-23
- **Founder decision:** Founder decided 2026-07-23 that the server is **EN+CN
  only**: no ja/ko locale axis and no new fact regions. This retires the §V57
  extra-locale NAME-alias feature (v0.2 M10) rather than refining a D1–D15
  decision — §V57 itself is marked RETIRED and its behavior removed.
- **Invariants:** §V57, §V50, §V21, §V37, §V5, §V73
- **Continues:** the v0.2 response-shape line (ADR 0011 → 0012). This ADR is the
  M15 bundle anchor: the M15 breaking reshapes that follow (T158 per-row region
  hoist, T161 item-drops efficiency trim, T168 module effect dedup) record here.

## Context

v0.2 M10 (T98–T101, T108–T109) added an **extra-locale NAME-alias** axis: the
`arknights_extra_locale_names` source imported the per-locale canonical NAME
(jp/kr) from a gamedata mirror and attached it as a locale-tagged search alias on
the existing en/cn entity that shared its `game_id`. `search_entities` grew an
optional `locale` (`ja`/`ko`) filter, plus two availability verdicts guarding it
(`locale_unavailable` for a build with no alias in that locale, B66; and
`locale_not_applicable` for a `locale` filter scoped to an item/stage type with no
alias table, B77).

The axis never became a fact region — a `locale` match still returned the entity's
OWN en/cn facts (§V5/§V57 region integrity preserved). But it carried real cost:
a second network source with no shipped default URL, a per-locale sync ride-along,
a query-time `EXISTS` narrowing over the alias tables, two extra typed statuses,
and the tool/model/service/description surface that documented all of it.

Founder decided 2026-07-23 that the server is EN+CN only. With that, the ja/ko axis
is dead weight, and the `locale=en`/`locale=zh` degenerate/asymmetric cases (B50)
confirm the axis was only ever meaningful for the jp/kr NAME aliases now removed.

## Decision

1. **Drop the query axis.** Remove the `locale` parameter from
   `SearchEntitiesInput`, `services.search.search_entities`, and the repository
   `search()` — including the trailing `EXISTS` locale clause in the search SQL and
   the `has_locale_alias` availability gate (the B66/B77 code). Remove the
   `locale_unavailable` / `locale_not_applicable` statuses and their tool
   envelopes. `SearchEntitiesInput` is `extra="forbid"` (§V18), so a client still
   sending `locale` is rejected at the model gate rather than silently ignored.

2. **Remove the source.** Delete the `arknights_extra_locale_names` source adapter,
   its importer, and the `sync` ride-along, and remove the registry entry from
   `config/data_sources.toml` (+ example), `DATA_SOURCES.md`, and
   `config.example.toml`. Founder intent was "remove the source", not merely
   disable it (§V37: no orphaned dead path).

3. **Keep the schema.** Migrations 0011 (`locale` column) and 0012 (alias UNIQUE
   index) STAY — the alias tables remain, now carrying only the operator en/zh
   self-aliases (T98), which still feed the FTS `name` document at build time so an
   operator stays matchable by appellation. `enemy_aliases` is simply empty (the
   primary enemy importer never inserted a self-alias, B50). `REGION_TO_NAME_LOCALE`
   STAYS — it has two live consumers unrelated to the ja/ko axis: penguin item
   `name_i18n` display (§V59) and the operator self-alias locale stamp (T98).

4. **No `schema_version` bump.** v0.2 is still unreleased (external release gated on
   T146 per ADR 0012 / B69). Removing an OPTIONAL input parameter folds into the
   still-unreleased v0.2 line; `SCHEMA_VERSION` stays `"0.2"`.

5. **Preserve item search (§V73).** The extra-locale ride-along's `after_all` was
   the only step that rebuilt `entity_fts` AFTER the penguin ride-along imports the
   `items` table — the pipeline builds the index before items exist. Removing the
   ride-along would leave penguin items unindexed and dead-end the `get_item_drops`
   name→id path (B83). The FTS rebuild is relocated into `sync`'s post-build step
   (`_reindex_after_ride_alongs`) so items are searchable regardless of any optional
   source, fail-open under a savepoint (§V58, never blocks the promote).

## Consequences

- ja/ko NAME search is no longer available; searching a katakana/hangul name no
  longer resolves the en/cn entity. This is the intended EN+CN-only posture.
- The search surface is smaller and the query path has no per-hit alias `EXISTS`
  narrowing; en/cn name/alias/code/id/tag search is unchanged.
- `entity_fts` is now rebuilt after every `sync` ride-along, which also fixes the
  latent gap where penguin items were only indexed when the extra-locale source
  happened to be configured (§V73/B83).
- History for the retired feature lives in git (T98–T101, T108–T109) and in the
  §B backprop rows (B50/B51/B52/B66/B76/B77) it resolved.

## M15 reshape log

This ADR is the M15 bundle anchor (see **Continues**, above): the M15 breaking
response reshapes fold into the still-unreleased v0.2 line, so `SCHEMA_VERSION`
stays `"0.2"` (same reasoning as decision point 4 — v0.2 is external-release-gated
on T146 per ADR 0012 / B69). Each reshape is recorded here as it lands:

- **T158 — per-row `region` hoist (B79/§V77/§V66).** `get_stage_drops`,
  `get_item_drops`, `get_announcements`, and `get_banners` no longer stamp `region`
  on every drop / stage / announcement / banner row. Each response is single-region
  (`server` is a required selector, §V5), so region is stated ONCE: on the parent
  object (`data.stage.server` / `data.item.server` for the drop tools; a new
  `data.server` field on the `get_announcements` / `get_banners` list envelopes) plus
  the envelope provenance. A 50-row page dropped 50 redundant `region` fields (§V66
  economy). Wire-only, mirroring T157 (B78): the domain `*Facts.region` fields stay
  (they carry the §V5 attribution the acceptance tests verify and back the parent /
  provenance derivation), and the `region` DB column is untouched.

- **T161 — `get_item_drops` efficiency dual-shape trim (B82/§V66/§V22).** A live
  `include_efficiency=true` response emitted BOTH a `stages[]` facts list AND an
  `efficiency.observation.ranking[]` that re-listed the same stages (id + name twice,
  two full per-stage objects) ≈ 2× payload. The ranking now **subsumes** the stage
  rows: with `include_efficiency` the ranked observation is the SINGLE per-stage list —
  each ranking row folds the raw drop facts (`sanity_cost` / `quantity` / `times` /
  `drop_rate`, the §V55 evidence) plus its derived `sanity_per_item`, keyed by the
  unambiguous `id` = `stage_game_id` with `stage_code` as `name` (§V68), and carries the
  hoisted `drop_provenance` deviations + `expired` (§V66.2/§V67) — so the top-level
  `stages[]` / `stages_page` are omitted and the response never lists the same stages
  twice. Without the flag (or when nothing is rankable) the raw `stages[]` facts + their
  `stages_page` are returned unchanged. The `efficiency_page` cursor pages the ranking;
  `stages_page` no longer applies in efficiency mode. Breaking response-shape change that
  folds into the still-unreleased v0.2 line, so `SCHEMA_VERSION` stays `"0.2"`.
