# ADR 0011: Response-shape v0.2 — one coordinated `schema_version` bump for the M13 breaking wire changes

- **Status:** Accepted
- **Date:** 2026-07-22
- **Founder decision:** none changed — no D1–D15 decision is reversed or refined. This
  is a §V21-mandated wire-contract ADR: §V21/§V25 (and `AGENTS.md`) forbid bumping
  `schema_version` without an ADR, so a breaking envelope reshape needs this record
  even though it touches no founder decision.
- **Invariants:** §V21, §V66, §V67, §V71, §V22, §V14
- **Numbering note:** the SPEC §T row for T128 labels this "ADR 0010", but 0010 was
  already taken by the effect-description-template ADR (T127). ADR numbers are
  monotonic and never reused (same rule as task ids), so this is **0011**.

## Context

M13 collects a set of tool-response improvements found during the live
tool-response comprehension review (`§B` B56–B61) and the payload-economy work
(§V66). Several are backward-compatible additions, but four are **breaking**
changes to the wire shape of the typed envelope (`mcp/envelopes.py`):

1. **Efficiency-observation compaction → ranked-single (§V66.1, T129).** The
   farming analyzer currently emits one observation *per ranked entity* — e.g.
   `get_item_drops(include_efficiency)` re-states an identical `rule_id` /
   `category` / `title` / `confidence` / `analyzer_version` block ~22 times over a
   ranked stage set (~200 tokens each). §V66.1 collapses this to **one** ranked
   observation carrying `ranking` rows `{id, name, sanity_per_item}`, with the §V6
   fields stated once at the observation level and per-row `confidence` /
   `limitation` only where a row deviates (thin sample, expired). The observation
   count and nesting change, so a client that parsed "one observation per stage"
   breaks.

2. **Provenance hoist (§V66.2, T130).** Drop rows today each carry an identical
   per-row provenance block (`snapshot_id` / `fetched_at` / `expires_at` /
   `imported_at`). §V66.2 hoists the shared block once and keeps a per-row field
   only where the row deviates (a different snapshot, or `expired:true`). The
   per-row provenance location moves, so a client reading `row.provenance` breaks.

3. **camelCase → snake_case rename (§V71.d).** Four upstream camelCase keys leak
   onto the wire unchanged today — verified in the live surface: `valueStr` (in the
   `blackboard` / `stat_bonus` items of `get_operator` and
   `compare_operator_modules`), `talentIndex` (in `compare_operator_modules`
   `talent_changes`), and `unlockCondition` / `requiredPotentialRank` (in the
   `get_operator` talent variants). §V71.d requires wire field names to be
   snake_case, normalized at the shaping layer. Renaming a required field is a
   breaking change (§V21).

4. **Columnar per-level arrays (§V67).** §V67 notes that per-level scalar arrays
   (`{sp_cost:[40,40,35,…]}`) would let a client see a trend at a glance and cut
   repetition, but this is a **breaking** reshape of the per-level blocks and §V67
   explicitly defers it "behind the T128 ADR".

§V21 is unambiguous: a breaking change to a required field bumps `SCHEMA_VERSION`
(currently `"0.1"`) and requires an ADR, while additive optional fields do **not**
bump it. Left uncoordinated, each breaking change above would independently trip
the §V21 bump rule, churning the version `0.1 → 0.2 → 0.3 → 0.4` across a single
milestone and forcing clients to re-migrate three or four times for what is one
logical wire-contract revision. §V66.1, §V66.2, §V67, and §V71.d each name **this
ADR (T128)** as the coordination point precisely to avoid that.

## Decision

Treat the M13 breaking envelope changes as **one** wire-contract revision,
**v0.2**, gated by a **single** `SCHEMA_VERSION` bump `"0.1" → "0.2"`.

### One bump for the whole bundle

`SCHEMA_VERSION` is bumped exactly once, from `"0.1"` to `"0.2"`, to cover the
entire coordinated bundle. No individual breaking task bumps the constant on its
own; there is no intermediate `0.1.x` or per-change version. This single bump is
the §V21/§V25 ADR-gated event, and this ADR is that gate.

### The coordinated breaking bundle (all land under v0.2)

- **Ranked-single efficiency observation (§V66.1, T129):** one ranked observation
  per rule with `ranking` rows `{id|stage/item id, name, sanity_per_item}`; §V6
  fields (`rule_id` / `confidence` / `analyzer_version`) stated once at the
  observation level; per-row `confidence` / `limitation` only where deviating.
  Evidence **references** the sibling facts list (drops already carry
  `sanity_cost` / `drop_rate` / `sample_size`) rather than re-copying the numbers.
  Applies to `get_stage_drops` and `get_item_drops`. Ranking stays ascending and
  the §V7/§V55 no-"best-farm" discipline is unchanged.
- **Provenance hoist (§V66.2, T130):** one shared `drop_provenance` block per
  response; a per-row field appears only where the row deviates (different
  snapshot, or `expired:true`) so the deviant row stays visible rather than buried.
- **camelCase → snake_case rename (§V71.d):** normalize the four leaked keys at the
  shaping layer — `valueStr → value_str`, `talentIndex → talent_index`,
  `unlockCondition → unlock_condition`, `requiredPotentialRank →
  required_potential_rank`. The rename is a **wire/shaping** concern only; the
  importer and stored fragments may keep upstream key names (allowlist unchanged,
  no `FIELD_POLICY_VERSION` bump) — the normalization happens where the envelope is
  built. T134 defers the rename to this ADR; it lands with the v0.2 bundle, not as
  T134 client-text polish.

### Deferred — evaluated, not shipped in v0.2

- **Columnar per-level arrays (§V67):** evaluated as a real token-economy win, but
  it is a broad breaking reshape of every per-level block (skills, talents,
  modules) and would enlarge the v0.2 review surface well beyond the drop/observation
  and rename changes above. **Deferred** out of the v0.2 bundle to keep this bump
  scoped and reviewable. It remains a candidate for a future wire-contract ADR and a
  later `schema_version` bump; §V67 keeps it gated. No columnar reshape ships under
  v0.2.

### Coordination rule — the version never advertises a shape it does not emit

The bundle is developed and released **together** as v0.2 within milestone M13;
there is no external release between the individual breaking tasks (T129, T130, the
§V71.d rename). The `SCHEMA_VERSION` constant flip to `"0.2"` — together with
updating the envelope/contract tests (`test_envelopes.py`, the per-tool tests, the
MCP-inspector contract test) that pin `"0.1"` — lands **with the bundle's
completion**, performed by the task that lands the last breaking change in the
bundle (or a dedicated coordination commit), never before a shape it advertises
actually emits. Fail-safe direction: the version tag trails the shape within the
milestone and flips once, so no shipped v0.2 client ever sees a `0.1` tag on a
reshaped payload or a `0.2` tag on an un-reshaped one.

### Additive items are NOT gated — they land independently

The following M13 items are backward-compatible additions (a client that ignores
them is unaffected) and are **§V21-safe without a bump**. They must **not** bump
`schema_version` (bumping on an additive change would violate the "one bump"
discipline above) and are free to land in any order, independent of the v0.2
bundle:

- **null-key omission / list discipline (§V67, T130):** `[]` = confirmed-none, an
  absent key = not-in-source, and always-null optional scalars (`valueStr` once
  renamed, `range_id`, `spawn_group`) are omitted rather than emitted as `null`;
  the convention is stated in the tool descriptions. (Omitting an always-null
  optional key is additive-safe per §V21; only the *rename* of a present field is
  breaking.)
- **search locator variant `difficulty` tag (§V70, T133).**
- **cost-item `display_name` pairing (§V69, T132).**
- **unit words in numeric-field descriptions (§V71.e, T134).**
- **standing limitations** — the §V65 grounding floor (T126), the §V67
  absent-expected-field limitation (T130), and the §V72 image-refs
  derived-unverified limitation (T135).

Widening the breaking bundle later — e.g. shipping the deferred columnar arrays, or
any further required-field reshape — requires its own wire-contract ADR and its own
coordinated `schema_version` bump; it must not be smuggled in as an additive change.

## Consequences

- Clients migrate to the v0.2 envelope **once** for the whole reshape (ranked
  observations, hoisted provenance, snake_case fields) instead of chasing three or
  four separate version bumps across one milestone.
- The ranked-single observation and provenance hoist materially cut payload size
  for the drop tools, tightening the §V22 economy that §V66 extends, while keeping
  the §V6 evidence discipline intact (fields stated once, deviations still visible).
- The wire is uniformly snake_case (§V71.d); the last upstream camelCase leaks
  (`valueStr` / `talentIndex` / `unlockCondition` / `requiredPotentialRank`)
  disappear from the client contract. The rename is confined to the shaping layer,
  so the field allowlist and `FIELD_POLICY_VERSION` are unchanged.
- Columnar per-level arrays stay a known, evaluated future option; deferring them
  keeps the v0.2 review surface small and the single bump defensible.
- Additive polish (variant tag, name pairing, units, limitations, null-key
  omission) keeps flowing without gating, so the rest of M13 is not blocked on the
  breaking bundle.
- The single-bump discipline is now enforceable: no task in the bundle bumps
  `schema_version` on its own, additive changes never bump it, and any future
  breaking reshape must return here for a new coordinated ADR + bump. Both
  transports emit the reshaped envelope through the one shared service (§V14), so
  the v0.2 shape is identical on `stdio` and Streamable HTTP.
