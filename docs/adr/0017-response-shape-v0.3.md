# ADR 0017: Response shape v0.3 — one coordinated `schema_version` 0.2 → 0.3

- **Status:** Accepted
- **Date:** 2026-07-29
- **Invariants:** §V21, §V99, §V100, §V71, §V106
- **Continues:** the response-shape ADR line (0011 → 0012 → 0013 → 0014). This ADR
  **opens a new version** rather than widening v0.2; the reasoning is in Context.

## Context

Five §V-mandated wire fixes accumulated in §B, each filed with the same disposition —
*"BREAKING → the T198 bump"*. They were parked deliberately: §V21 forbids a breaking
reshape without a coordinated `schema_version` flip plus an ADR, and B69 is the record of
what happens when that flip is treated as a footnote. There, `SCHEMA_VERSION` stayed
`"0.1"` while five reshaped payloads shipped under it, because the flip lived inside the
last bundle member instead of being its own tracked task. §T198 is that tracked row.

### Why this mints 0.3 instead of folding into v0.2

ADRs 0011 → 0014 each *folded* a breaking reshape into v0.2 and left the constant alone.
That was never a general rule; it rested on one specific fact recorded in ADR 0012:

> **Release gate (binding):** do not cut an external v0.2 release until T146 has landed.

The fold was legitimate precisely because no external client had been promised the interim
v0.2 shape — 0013 and 0014 both cite that gate by name. **T146 has since landed**, so the
gate is discharged and v0.2 is a complete, releasable shape rather than a moving target.
Continuing to mutate it would recreate exactly the condition §V21 exists to prevent: a
stable tag over a payload that keeps changing underneath it. So the v0.2 line is closed as
shipped, and this bundle opens v0.3.

### What each member fixes

Every figure below was counted against the promoted build
`2026-07-29T083659Z-en-cn.sqlite`, not estimated.

## Decision

Land the five reshapes together under **one** `SCHEMA_VERSION` flip, `"0.2"` → `"0.3"`.

- **Item `rarity` → int, 1-indexed (§V99/B133).** One wire key carried two JSON types AND
  two numeric bases: `operators.rarity` is an INTEGER 1..6 (the in-game star count, 1-indexed
  and correct), while `items.rarity` was TEXT `"0".."4"` — a 0-indexed tier (Orirock `"0"`
  = T1, Orirock Cube `"1"` = T2, D32 Steel `"4"` = T5). Neither was documented, so a client
  could not tell a 1★ operator from a T2 material by the field alone. Both now emit the
  number a player actually sees: an **int, 1-indexed**, so §V99's "same JSON type AND same
  numeric base, else RENAME one" is satisfied without renaming either key, and each
  emitting tool's description states the scale. The conversion is a READ-side bridge
  (`services/drops.py:_item_rarity_tier`, the one §V37 home both drop directions already
  funnel through); the importer still stores what penguin sent, so **no stored byte
  changes and no §V92 promote is owed**.

- **Ranking rows entity-prefixed (§V100/B134).** `get_stage_drops.ranking[]` emitted
  `{id: "30012", name: "Orirock Cube"}` (an ITEM id + display name) while the sibling
  `get_item_drops.ranking[]` emitted `{id: "main_01-07", name: "1-7"}` (a STAGE id + a
  stage CODE). Same shape, flipped referents, and one `name` that was not a name — so an
  LLM mislabelled the column it was rendering. The rows now carry
  `item_game_id`/`item_display_name` and `stage_game_id`/`stage_code`: a `*_name` key must
  hold a display name and a code lives in `*_code` (§V100). Both emitters already built
  exactly those keys and then re-keyed them away; the re-key is deleted, not remapped. The
  generic `id`/`name` slots survive only inside the analyzer's `RankingRow`, which never
  reaches the wire, and a contract test walks every registered tool's `ranking` rows to
  keep it that way.

- **Change-bundle keys snake_case + one phase encoding (§V71 d/B140).** Module and talent
  change bundles shipped `requiredPotentialRank` / `talentIndex` / `unlockCondition`
  straight off the upstream dump, three lines from a snake_case `unlock_phase` /
  `stat_bonus`. The same object also encoded the unlock phase TWICE, two ways:
  `unlock_phase: 2` (int) beside `unlockCondition.phase: "PHASE_2"` (str). §V71 (d) named
  these exact keys and gated the rename on T128's ADR; T128 closed and B69 spent the bump
  it was riding, so the fix lost its vehicle and the invariant has read "satisfied" ever
  since — a declared-but-undelivered clause, worse than an undeclared one. The keys are now
  normalized and the nested phase becomes the int its sibling already is, in
  `services/effect_changes.py:normalize_change_keys` — the last step of the shaping pair
  both read services share, so the dedup identity and the token label still read the
  source's own names. (That module is a §V38-forced split: `services/operators.py` had
  reached 767 lines against the 800-line cap.)

- **Empty SET query → `ok` (§V106/B147).** `search_entities("Amyia")` answered
  `not_found` while `get_announcements` answered `ok` + `[]` for an empty window, so a
  client branching on `status` read "no name matched" as a failure and "no rows in range"
  as a success. §V23 enumerated the statuses but never said which applies to an empty set,
  so each tool chose locally and consistently — the gap was that no rule spanned them. One
  rule now applies, keyed on question shape: a LOOKUP of a named entity that does not exist
  stays `not_found`; a SET QUERY with zero hits is `ok` + an empty collection + a limitation
  carrying the why and the next step. Flipped: both search tools, `get_item_drops` for a
  RESOLVED item with no drop cache, and `get_stage_drops` for a RESOLVED stage with no drop
  cache. Still `not_found`: an unknown item game_id, an absent stage. §V50's availability
  gates (`unsupported_server` / `data_stale`) are real errors and still fire first.
  No client-facing fact was deleted to make room — B91's craft/synthesis wording with its
  `get_data_status` pointer, and T195's shared-`stage_code` alternates, both MOVED from the
  error body into the limitation (§V111 b).

- **`get_data_status` namespace collisions (§V99/B148).** The response shipped
  `schema_version` twice with two unrelated meanings — the envelope's `"0.2"` response
  contract and `data.schema_version`'s `"0018_enemy_range_declared_none"` DB migration id —
  and echoed `status` and `analyzer_version` at both levels. The inner key becomes
  `db_schema_version` and the two echoes are dropped (§V66: the envelope is the sole
  carrier). A **third** collision found while verifying B148 rides the same bump rather
  than waiting for a future one: `snapshots[].status` = `"imported"` is a snapshot
  lifecycle state sharing its name with the envelope's §V23 result status, in the same
  payload, so it becomes `import_status`. The tool, the `arknights://status/{server}`
  resource, and the CLI `status --json` all read one new §V37 projection
  (`DataStatus.to_envelope_data`), so the three surfaces cannot re-fork.

## Consequences

- One tag, one shape: a v0.3 client reads all five fixes together, and no reshaped payload
  ever ships under a version that predates it (§V21; the B69 failure mode).
- **No rebuild.** Every member is emit-side; `FIELD_POLICY_VERSION` and
  `TRANSFORM_VERSION` are untouched and the promoted build stays valid, which is what makes
  a wire-only bundle cheap enough to land as one coordinated flip.
- A v0.2 client must move: `rarity` is an int one greater than the string it read;
  `ranking[].id`/`.name` are gone in favour of entity-prefixed keys; `talentIndex` and
  siblings are snake_case; a zero-hit search returns `ok` with an empty list rather than
  `not_found`; and `get_data_status` renamed two keys and dropped two echoes.
- The v0.2 line (ADR 0011 → 0014) is closed as shipped. A further breaking reshape opens
  v0.4 under its own tracked task and ADR — it does not widen v0.3.
- §V106's rule is now stated once and applied across every tool, so a future tool inherits
  it instead of picking its own empty-set status.
