# ADR 0015: Skin gallery import — named metadata in, prose and art out; alt-form via tmplId

- **Status:** Accepted
- **Date:** 2026-07-26
- **Amends:** the truthful-partial floor and the skin
  first-cut. The base-outfit-only `_1b`/`_2b` derivation stays as the fallback;
  on a build carrying this import the emitted skin gallery becomes complete and
  named. Derive-not-store, never-fetch, startup posture, and the kill switches
  are unchanged.
- **Founder decision(s):** D5-adjacent — extends the D5 URL-reference carve
  (ADR 0008/0009) from two derived base-outfit links to the full named gallery.
  Import scope stays inside D6/D11 (metadata-only; no prose, no art bytes).

## Context

From the bare derived `_1b`/`_2b` skin refs a client can neither build an
outfit gallery nor even state confidently that one is partial. The floor
shipped — a standing partial-gallery limitation so the deferral is visible. The
real fix was gated on an ADR: import `skin_table.json` metadata so
the gallery is complete and named.

Verified against the live upstream (`ArknightsAssets/ArknightsGamedata`, EN+CN)
and the live mirror (`yuanyan3060/ArknightsGameResource`) on 2026-07-26:

- `charSkins` is an id-keyed dict (EN 2048 / CN 2306 entries). Each entry
  carries `skinId`, `charId`, `portraitId`, `tmplId`, `isBuySkin`, and a
  `displaySkin` block mixing wanted metadata (`skinName`, `skinGroupId`,
  `skinGroupName`) with forbidden prose (`content`, `dialog`, `usage`,
  `description`, artist `drawerList`).
- **Mirror rule:** every skin illustration — default E0/E1/E2 art, paid/event
  outfits, and alt-form art alike — lives at `skin/<portraitId>b.png`
  (six HEAD probes all 200, including `…_epoque#4b`, `…_1+b`,
  `char_1001_amiya2_sale#16b`). The existing unconditional `#`/`+` encoder
  covers the stems.
- **Alt-form link:** alt-form skins (`char_1001_amiya2#2`,
  `char_1037_amiya3#2`, …) carry the BASE operator's `charId`
  (`char_002_amiya`) plus a distinct `tmplId` naming the form. `tmplId` equals
  `charId` everywhere else. So `skin_table` itself carries the alt-form
  discriminator.
- **Token skins:** 767 EN entries have a non-`char_` `charId` (summon/token
  art) — not part of an operator gallery. Every remaining operator entry has a
  `portraitId`.

## Decision

1. **Import `charSkins` metadata only.** New optional domain `operator_skins`
   (migration 0014): skin id, char id, tmpl id, portrait id, display name,
   skin-group id/name, paid flag, region, provenance.
   `SKIN_ALLOWLIST` + `DISPLAY_SKIN_ALLOWLIST` (nested-parent sub-extraction,
   the `dynMeta` pattern) with `FIELD_POLICY_VERSION` bumped `"6"` → `"7"`.
   **No prose column exists** — `content`/`dialog`/`usage`/`description`/
   `drawerList`/`modelName` are absent from allowlist and schema alike
   (ceiling).
2. **Token skins excluded** at parse (`charId` not `char_`-prefixed).
3. **Optional, fail-open domain** (the banners pattern):
   `skin_table.json` joins `SUPPLEMENTARY_FILES` (tolerant-absent; the
   introspection test pins the wiring), the import runs after operators under
   its own savepoint, and an `ImporterError` (non-empty-to-zero guard,
   duplicate `skinId`) skips the domain while the combat build continues.
   `operator_skins` stays out of `CRITICAL_TABLES`.
4. **Alt-form via `tmplId`, not `char_patch_table.json`.** The task's optional
   char-patch import is rejected: `tmplId` alone carries the link. Alt-form
   skins soft-resolve through their base `charId` to the base operator and are
   emitted **explicitly labeled `alt_form`** — never silently folded. The reading
   this ratifies: "never imply the base operator covers alt forms" is
   satisfied by the per-ref label + a standing alt-form note (emitted only when
   an alt-form ref is present); alt-form operators themselves stay out of the
   search index and are not separately fetchable.
5. **URL derivation unchanged in kind**: `skin/<portraitId>b.png` built
   at response time from the imported `portrait_id` through the single shared
   encoder; no URL and no byte stored; the server never fetches.
6. **Wire shape additive** (no `schema_version` bump): each named skin
   ref extends `{category, path, variant, source_id}` (the ADR 0014 hoist
   shape — the former per-ref `url` is a relative `path` under the response's
   `image_refs_base_url`) with `skin_id` always and `skin_name`/`skin_group`
   when named; `alt_form`/`paid` only when true (omit-discipline).
   `variant` maps `ILLUST_0/1/2` → `e0`/`e1`/`e2`, named outfits → `skin`;
   on an `alt_form` ref those labels name the alternate form's art.
7. **Fallback preserved**: a build without the skin domain (pre-0014
   active DB, combat-only snapshot) keeps the derived base `_1b`/`_2b` refs and
   the partial-gallery limitation; the repository degrades a missing
   `operator_skins` table to zero rows so an old active DB never errors. On the
   named path the partial-gallery limitation is dropped — the outfit list is
   complete; the derived-unverified limitation still rides every emit.

## What does NOT change

- No art bytes, no prose — in the release, the DB, or the wire.
- Never fetch; derive, don't store; takedown = config flip.
- Refs attach to one already-fetched operator; no catalog/enumeration.
- Search index: skins are not indexed; alt-form operators stay unsearchable.
- `envelopes.SCHEMA_VERSION` (`0.2`): all wire additions are additive-optional.
- Enemy and banner ref surfaces (no skin category on either).

## Consequences

- A rebuild (sync/import at schema 0014) is required before the named gallery
  appears; until then every response takes the fallback lane.
- Paid/event outfits are now present and labeled; EN/CN gallery gaps are
  expected and region-scoped.
- Mirror lag / dead links remain possible and remain disclosed.
- Purge cascades `operator_skins` ahead of the operator domain.

## Alternatives considered

- **Import `char_patch_table.json` for the alt-form link** — rejected:
  `tmplId` suffices for labeling; promoting alt forms to full operators is a
  separate, larger decision.
- **Exclude alt-form skins entirely** — rejected: they are the exact "Amiya
  skins" gap reported; labeling beats omission.
- **Index skin names in `entity_fts`** — rejected (YAGNI): refs
  attach to an already-resolved operator; a name-search axis for outfits has no
  driving use case.
