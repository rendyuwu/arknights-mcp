# ADR 0014: Fold the M16 breaking response reshapes into unreleased v0.2

- **Status:** Accepted
- **Date:** 2026-07-26
- **Continues:** the v0.2 response-shape line (ADR 0011 → 0012 → 0013). This ADR is
  the M16 bundle anchor: the M16 breaking response reshapes (stage-drops
  efficiency fold; `image_refs` base-url hoist when it lands) record here.

## Context

M16 continues the payload-economy sweep the M13–M15 evals drove. Most M16
tasks are additive or emit-only (safe), but a reshape that *removes or moves* a
field a v0.2 client could already be reading is breaking and needs an ADR +
`schema_version` decision.

The precedent is set by ADR 0012/0013: v0.2 is still **unreleased** — it is
external-release-gated per ADR 0012, so no external client has ever been
promised the interim v0.2 shape. A breaking reshape that lands before that gate folds
into the v0.2 line and `SCHEMA_VERSION` stays `"0.2"`; there is no `0.3` bump for a
shape no external client ever consumed.

## Decision

M16 breaking reshapes fold into the still-unreleased v0.2 line; `SCHEMA_VERSION`
stays `"0.2"`. Each reshape is recorded here as it lands:

- **`get_stage_drops` efficiency fold.** A live
  `include_efficiency=true` response emitted BOTH a `drops[]` facts list AND an
  `efficiency.observation.ranking[]` that re-listed the same items (id + name twice;
  19 items in the eval response). The sibling `get_item_drops` fold landed first
  — the same one-surface-not-both miss class. The ranking now
  **subsumes** the drop rows, mirroring that fold: with `include_efficiency` the ranked
  observation is the SINGLE per-item list — each ranking row folds the raw drop facts
  (`quantity` / `times` / `drop_rate` — plus `item_rarity` /
  `item_type`) and its derived `sanity_per_item`, keyed by the unambiguous
  `id` = `item_game_id` with the item's display name as `name`, and
  carries the hoisted `drop_provenance` deviations + `expired` — so the
  top-level `drops[]` is omitted and the response never lists the same items twice.
  The stage-level `sanity_cost` is NOT repeated per row; it rides the parent `stage`
  block once (it is constant for one stage, unlike the item view where it
  varies per stage). Without the flag (or when nothing is rankable — no `sanity_cost`,
  or every `drop_rate` absent/non-positive) the raw `drops[]` facts are returned
  unchanged, with warnings naming any exclusion. Breaking response-shape
  change that folds into the still-unreleased v0.2 line, so `SCHEMA_VERSION` stays
  `"0.2"`.

- **`image_refs` base-url hoist.** Every emitted ref repeated the
  full 66-char mirror base (`https://raw.githubusercontent.com/yuanyan3060/
  ArknightsGameResource/main`) inside its absolute `url` — a full operator gallery
  (portrait 2 + avatar 2 + N skins) restated it 7+ times, and a banner page once per
  ref per resolved featured op. The hoist rule (identical per-row value → one
  shared block) now applies: each ref carries a RELATIVE `path`
  (`portrait/char_002_amiya_1.png`) and the response emits the shared base ONCE as a
  `data`-level `image_refs_base_url` on all three ref-bearing tools
  (`get_operator` / `get_enemy` / `get_banners`, one uniform lookup — the same-shape
  rule); the client joins `base_url + "/" + path` for the full URL, and
  each tool description says so. The base key is present exactly when the response
  emits ≥1 ref (absent otherwise, same predicate as the standing
  limitation). The derive-never-fetch rule is untouched: paths stay query-time
  DERIVED, never stored, never fetched, percent-encoding unchanged. Breaking
  (per-ref `url` removed/renamed) → folds into the still-unreleased v0.2 line, so
  `SCHEMA_VERSION` stays `"0.2"`.

## Consequences

- One fold contract across both drop tools: a client reads the ranked observation as
  the single per-entity list in efficiency mode, in both directions of the penguin
  cache (same shape rule, no per-tool divergence to document around).
- The evidence discipline is unchanged: rule_id / baseline confidence / analyzer_version
  once at the observation level, per-row `confidence`/`expired`/`flags` only where a
  row deviates, hoisted duplicate sentences once in the observation limitations.
- A v0.2-interim client that read `drops[]` under `include_efficiency=true` must move
  to `efficiency.observation.ranking[]`; acceptable because v0.2 is unreleased (the
  release gate has not been passed).
