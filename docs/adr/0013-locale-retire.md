# ADR 0013: Retire the extra-locale (ja/ko) NAME-alias axis — EN+CN only

- **Status:** Accepted
- **Date:** 2026-07-23
- **Founder decision:** Founder decided 2026-07-23 that the server is **EN+CN
  only**: no ja/ko locale axis and no new fact regions. This retires the
  extra-locale NAME-alias feature (v0.2 M10) rather than refining a D1–D15
  decision — the feature is marked RETIRED and its behavior removed.
- **Continues:** the v0.2 response-shape line (ADR 0011 → 0012). This ADR is the
  M15 bundle anchor: the M15 breaking reshapes that follow (per-row region
  hoist, item-drops efficiency trim, module effect dedup) record here.

## Context

v0.2 M10 added an **extra-locale NAME-alias** axis: the
`arknights_extra_locale_names` source imported the per-locale canonical NAME
(jp/kr) from a gamedata mirror and attached it as a locale-tagged search alias on
the existing en/cn entity that shared its `game_id`. `search_entities` grew an
optional `locale` (`ja`/`ko`) filter, plus two availability verdicts guarding it
(`locale_unavailable` for a build with no alias in that locale; and
`locale_not_applicable` for a `locale` filter scoped to an item/stage type with no
alias table).

The axis never became a fact region — a `locale` match still returned the entity's
OWN en/cn facts (region integrity preserved). But it carried real cost:
a second network source with no shipped default URL, a per-locale sync ride-along,
a query-time `EXISTS` narrowing over the alias tables, two extra typed statuses,
and the tool/model/service/description surface that documented all of it.

Founder decided 2026-07-23 that the server is EN+CN only. With that, the ja/ko axis
is dead weight, and the `locale=en`/`locale=zh` degenerate/asymmetric cases
confirm the axis was only ever meaningful for the jp/kr NAME aliases now removed.

## Decision

1. **Drop the query axis.** Remove the `locale` parameter from
   `SearchEntitiesInput`, `services.search.search_entities`, and the repository
   `search()` — including the trailing `EXISTS` locale clause in the search SQL and
   the `has_locale_alias` availability gate. Remove the
   `locale_unavailable` / `locale_not_applicable` statuses and their tool
   envelopes. `SearchEntitiesInput` is `extra="forbid"`, so a client still
   sending `locale` is rejected at the model gate rather than silently ignored.

2. **Remove the source.** Delete the `arknights_extra_locale_names` source adapter,
   its importer, and the `sync` ride-along, and remove the registry entry from
   `config/data_sources.toml` (+ example), `DATA_SOURCES.md`, and
   `config.example.toml`. Founder intent was "remove the source", not merely
   disable it — no orphaned dead path.

3. **Keep the schema.** Migrations 0011 (`locale` column) and 0012 (alias UNIQUE
   index) STAY — the alias tables remain, now carrying only the operator en/zh
   self-aliases, which still feed the FTS `name` document at build time so an
   operator stays matchable by appellation. `enemy_aliases` is simply empty (the
   primary enemy importer never inserted a self-alias). `REGION_TO_NAME_LOCALE`
   STAYS — it has two live consumers unrelated to the ja/ko axis: penguin item
   `name_i18n` display and the operator self-alias locale stamp.

4. **No `schema_version` bump.** v0.2 is still unreleased (external release gated
   per ADR 0012). Removing an OPTIONAL input parameter folds into the
   still-unreleased v0.2 line; `SCHEMA_VERSION` stays `"0.2"`.

5. **Preserve item search.** The extra-locale ride-along's `after_all` was
   the only step that rebuilt `entity_fts` AFTER the penguin ride-along imports the
   `items` table — the pipeline builds the index before items exist. Removing the
   ride-along would leave penguin items unindexed and dead-end the `get_item_drops`
   name→id path. The FTS rebuild is relocated into `sync`'s post-build step
   (`_reindex_after_ride_alongs`) so items are searchable regardless of any optional
   source, fail-open under a savepoint (never blocks the promote).

## Consequences

- ja/ko NAME search is no longer available; searching a katakana/hangul name no
  longer resolves the en/cn entity. This is the intended EN+CN-only posture.
- The search surface is smaller and the query path has no per-hit alias `EXISTS`
  narrowing; en/cn name/alias/code/id/tag search is unchanged.
- `entity_fts` is now rebuilt after every `sync` ride-along, which also fixes the
  latent gap where penguin items were only indexed when the extra-locale source
  happened to be configured.
- History for the retired feature lives in git.

## M15 reshape log

This ADR is the M15 bundle anchor (see **Continues**, above): the M15 breaking
response reshapes fold into the still-unreleased v0.2 line, so `SCHEMA_VERSION`
stays `"0.2"` (same reasoning as decision point 4 — v0.2 is external-release-gated
per ADR 0012). Each reshape is recorded here as it lands:

- **Per-row `region` hoist.** `get_stage_drops`,
  `get_item_drops`, `get_announcements`, and `get_banners` no longer stamp `region`
  on every drop / stage / announcement / banner row. Each response is single-region
  (`server` is a required selector), so region is stated ONCE: on the parent
  object (`data.stage.server` / `data.item.server` for the drop tools; a new
  `data.server` field on the `get_announcements` / `get_banners` list envelopes) plus
  the envelope provenance. A 50-row page dropped 50 redundant `region` fields.
  Wire-only, mirroring the earlier wire-only move: the domain `*Facts.region` fields stay
  (they carry the attribution the acceptance tests verify and back the parent /
  provenance derivation), and the `region` DB column is untouched.

- **`get_item_drops` efficiency dual-shape trim.** A live
  `include_efficiency=true` response emitted BOTH a `stages[]` facts list AND an
  `efficiency.observation.ranking[]` that re-listed the same stages (id + name twice,
  two full per-stage objects) ≈ 2× payload. The ranking now **subsumes** the stage
  rows: with `include_efficiency` the ranked observation is the SINGLE per-stage list —
  each ranking row folds the raw drop facts (`sanity_cost` / `quantity` / `times` /
  `drop_rate`) plus its derived `sanity_per_item`, keyed by the
  unambiguous `id` = `stage_game_id` with `stage_code` as `name`, and carries the
  hoisted `drop_provenance` deviations + `expired` — so the top-level
  `stages[]` / `stages_page` are omitted and the response never lists the same stages
  twice. Without the flag (or when nothing is rankable) the raw `stages[]` facts + their
  `stages_page` are returned unchanged. The `efficiency_page` cursor pages the ranking;
  `stages_page` no longer applies in efficiency mode. Breaking response-shape change that
  folds into the still-unreleased v0.2 line, so `SCHEMA_VERSION` stays `"0.2"`.

- **Module effect dedup + token label.** A module's per-level
  `trait_changes` / `talent_changes` no longer emit N near-identical rows for one change,
  no longer leave a `-1` summon/token change unlabelled, and no longer repeat a bundle
  byte-identical across levels. Three shared emit-shaping helpers route both
  `get_operator(include_modules)` and `compare_operator_modules`:
  (1) **dedup** — bundles sharing an identity `(talentIndex, requiredPotentialRank,
  unlockCondition)` merge into one row carrying the union of their non-empty fields
  (blackboard + description), byte-lossless (a genuine conflict — two distinct non-empty
  blackboards under one gate — keeps the rows separate); (2) **token label** — a
  `talentIndex == -1` change gains `applies_to: "token"` (it affects the operator's
  summon/token, not the operator); (3) **cross-level hoist** — a change bundle
  byte-identical across every present level rides the module once (new `trait_changes` /
  `talent_changes` fields on the module, alongside the existing `trait_change_description`)
  and is omitted from each level (omit-key, never null). The module analyzer still
  reads the raw decode, so observation counts are unaffected. Breaking response-shape change
  that folds into the still-unreleased v0.2 line, so `SCHEMA_VERSION` stays `"0.2"`.
