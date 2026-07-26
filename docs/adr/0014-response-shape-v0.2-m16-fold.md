# ADR 0014: Fold the M16 breaking response reshapes into unreleased v0.2

- **Status:** Accepted
- **Date:** 2026-07-26
- **Invariants:** §V66, §V22, §V21
- **Continues:** the v0.2 response-shape line (ADR 0011 → 0012 → 0013). This ADR is
  the M16 bundle anchor: the M16 breaking response reshapes (T176 stage-drops
  efficiency fold; T183 `image_refs` base-url hoist when it lands) record here.

## Context

M16 continues the §V66/§V22 payload-economy sweep the M13–M15 evals drove. Most M16
tasks are additive or emit-only (§V21-safe), but a reshape that *removes or moves* a
field a v0.2 client could already be reading is breaking by §V21 and needs an ADR +
`schema_version` decision.

The precedent is set by ADR 0012/0013: v0.2 is still **unreleased** — it is
external-release-gated (T146 per ADR 0012/B69), so no external client has ever been
promised the interim v0.2 shape. A breaking reshape that lands before that gate folds
into the v0.2 line and `SCHEMA_VERSION` stays `"0.2"`; there is no `0.3` bump for a
shape no external client ever consumed.

## Decision

M16 breaking reshapes fold into the still-unreleased v0.2 line; `SCHEMA_VERSION`
stays `"0.2"`. Each reshape is recorded here as it lands:

- **T176 — `get_stage_drops` efficiency fold (B95/§V66/§V22).** A live
  `include_efficiency=true` response emitted BOTH a `drops[]` facts list AND an
  `efficiency.observation.ranking[]` that re-listed the same items (id + name twice;
  19 items in the eval response). B82/T161 folded only the sibling
  `get_item_drops` — the same one-surface-not-both miss class as B42. The ranking now
  **subsumes** the drop rows, mirroring T161: with `include_efficiency` the ranked
  observation is the SINGLE per-item list — each ranking row folds the raw drop facts
  (`quantity` / `times` / `drop_rate` — the §V55 evidence — plus `item_rarity` /
  `item_type`) and its derived `sanity_per_item`, keyed by the unambiguous
  `id` = `item_game_id` with the item's display name as `name` (§V68/§V69), and
  carries the hoisted `drop_provenance` deviations + `expired` (§V66.2/§V67) — so the
  top-level `drops[]` is omitted and the response never lists the same items twice.
  The stage-level `sanity_cost` is NOT repeated per row; it rides the parent `stage`
  block once (§V66/§V77 — it is constant for one stage, unlike the item view where it
  varies per stage). Without the flag (or when nothing is rankable — no `sanity_cost`,
  or every `drop_rate` absent/non-positive) the raw `drops[]` facts are returned
  unchanged, with the §V26 warnings naming any exclusion. Breaking response-shape
  change that folds into the still-unreleased v0.2 line, so `SCHEMA_VERSION` stays
  `"0.2"`.

- **T183 — `image_refs` base-url hoist (§V66/§V22).** Every emitted ref repeated the
  full 66-char mirror base (`https://raw.githubusercontent.com/yuanyan3060/
  ArknightsGameResource/main`) inside its absolute `url` — a full operator gallery
  (portrait 2 + avatar 2 + N skins) restated it 7+ times, and a banner page once per
  ref per resolved featured op. The §V66.2 hoist rule (identical per-row value → one
  shared block) now applies: each ref carries a RELATIVE `path`
  (`portrait/char_002_amiya_1.png`) and the response emits the shared base ONCE as a
  `data`-level `image_refs_base_url` on all three ref-bearing tools
  (`get_operator` / `get_enemy` / `get_banners`, one uniform lookup — the B28
  same-shape rule); the client joins `base_url + "/" + path` for the full URL, and
  each tool description says so. The base key is present exactly when the response
  emits ≥1 ref (§V67 — absent otherwise, same predicate as the §V72 standing
  limitation). §V63 is untouched: paths stay query-time DERIVED, never stored, never
  fetched, percent-encoding unchanged. Breaking (per-ref `url` removed/renamed) →
  folds into the still-unreleased v0.2 line, so `SCHEMA_VERSION` stays `"0.2"`.

## Consequences

- One fold contract across both drop tools: a client reads the ranked observation as
  the single per-entity list in efficiency mode, in both directions of the penguin
  cache (§V37 — same shape rule, no per-tool divergence to document around).
- The §V6 discipline is unchanged: rule_id / baseline confidence / analyzer_version
  once at the observation level, per-row `confidence`/`expired`/`flags` only where a
  row deviates, hoisted §V85 sentences once in the observation limitations.
- A v0.2-interim client that read `drops[]` under `include_efficiency=true` must move
  to `efficiency.observation.ranking[]`; acceptable because v0.2 is unreleased (the
  T146 gate has not been passed).
