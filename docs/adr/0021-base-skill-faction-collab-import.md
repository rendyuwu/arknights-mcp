# ADR 0021: Base-skill, faction, and collab-flag import — RIIC mechanic text and faction ids in, handbook lore out

- **Status:** Accepted
- **Date:** 2026-09-29
- **Founder decision(s):** D4/D11 — extends the ADR 0010 mechanic-text carve to
  the base (RIIC) skill effect text. D5-adjacent — new data domains plus one
  new list tool (ADR 0007 and ADR 0015 are the precedents).

## Context

A roster owner building base (RIIC) combos needs each operator's base skills
(Bellone's "Famiglia Business α"), and needs to filter operators by facility
(Trading Post, Factory, …), by faction (Yan, Babel, …), and by collab
(Monster Hunter, …).

The account sync cannot supply this. `/account/syncData` returns player state
only: its `building` section holds room assignments and morale, not skill
definitions, and `importers/account.py` does not read it. ArkPRTS gets game
data either from Kengxxiao GitHub mirrors (`arkprts/assets/git.py`) or by
decrypting client asset bundles from the hot-update host
(`arkprts/assets/bundle.py`). ADR 0020 (c) blocks every asset or game-data
endpoint in the account client, and ADR 0020 (b) forbids ArkPRTS code.

The data is already in the primary source (`ArknightsAssets/ArknightsGamedata`,
same `gamedata/excel/` layout, EN and CN), verified on 2026-09-29:

- `building_data.json`: `chars.<charId>.buffChar[]` holds exactly 2 slots per
  char; each slot's `buffData[]` holds 0–3 progressive stages
  `{buffId, cond: {phase: "PHASE_n", level}}`. `buffs.<buffId>` carries
  `buffName`, `roomType` (9 values: CONTROL, DORMITORY, HIRE, MANUFACTURE,
  MEETING, POWER, TRADING, TRAINING, WORKSHOP), `description`, and display
  metadata (icons, colors, sort ids, targets). EN 410 chars / 715 buffs, CN
  429 / 755. Every stage buffId resolves; no slot spans two rooms.
  Descriptions are at most 440 chars, carry only `<@x.y>`/`<$x.y>`/`</>` tags,
  and no `{}` placeholders.
- `character_table.<id>.mainPower {nationId, groupId, teamId}` plus
  `subPower` (a list of the same shape, or null) name the factions; display
  names come from `handbook_team_table.json` (`powerId`, `powerName`).
- `handbook_info_table.json` `handbookDict.<charId>.isLimited` is true for
  exactly the collab operators (EN 22, CN 29). No field names the collab
  title. The rest of that file is lore.

## Decision

1. **Base-skill `description` is mechanic text.** It is short in-game effect
   text with no lore and no blackboard, so it is imported like the ADR 0010
   templates (read raw through `template_text`, rich-text tags stripped).
2. **Imported fields.** `buffId`, `buffName`, `roomType`, `description`
   (`BASE_SKILL_ALLOWLIST`); per stage `buffId` and `cond`
   (`BASE_SKILL_STAGE_ALLOWLIST`); faction ids from `mainPower`/`subPower`
   (`POWER_ALLOWLIST`); faction names (`HANDBOOK_TEAM_ALLOWLIST`); `isLimited`
   as the only field read from `handbook_info_table` (`HANDBOOK_INFO_ALLOWLIST`).
   `FIELD_POLICY_VERSION` `"15"` → `"16"`; migration 0021 adds `base_skills`,
   `operator_base_skills`, `operator_factions`, and `operators.collab`.
3. **Stays out.** Every story, profile, voice, and illustrator field of
   `handbook_info_table`; buff icons, colors, sort ids, and targets. No column
   exists for them.
4. **No account-sync change, no ArkPRTS code.** `sources/yostar.py` and
   `importers/account.py` are unchanged; the reasoning is in Context.
5. **Optional, fail-open domain** (the ADR 0015 pattern). The three files join
   `SUPPLEMENTARY_FILES` (tolerant-absent, same snapshot); the base-skill
   import runs under its own savepoint; the new tables stay out of
   `CRITICAL_TABLES`. A build predating 0021 degrades every new read to empty,
   and an empty answer from such a build carries a limitation asking for a sync.
6. **Read surface.** `get_operator include_base_skills` (plus `factions` and
   `collab` in the summary); a new `find_operators` list tool; `get_my_roster`
   gains `room_type`/`faction`/`collab` filters, and with `room_type` each row
   carries that facility's base skills with an `in_effect` flag computed from
   the synced elite and level.
7. **Enumeration posture.** `find_operators` is a walkable list, the same class
   as `get_banners`: every obtainable operator has a base skill, and the largest
   facility page on the pinned build is 92 rows, so nine `room_type` calls per
   region return the whole domain with its base-skill text. What bounds it is the
   per-call shape, not the filter: `page_size` is at most 100, rows are identity
   only unless `room_type` is set, and then carry only that facility's skills.
   The filter requirement (`room_type` or `faction`, or `collab=true`) only stops
   a no-filter call from paging every operator's identity; `collab=false` only
   narrows another filter.
8. **Fact/planning boundary.** The server returns facts: the skills, their
   unlock gates, and which stage is in effect for the account. It builds no
   combos, rankings, or recommendations.

## What does NOT change

- `envelopes.SCHEMA_VERSION`: every wire addition is additive, so there is no
  `schema_version` bump.
- The search index: nothing new is indexed.
- The account client, its allowlists, and `get_my_operator`.

## Consequences

- A rebuild (sync/import at schema 0021) is required before base skills,
  factions, and the collab flag appear.
- `TRANSFORM_VERSION` moves because every operator record now carries its
  faction blocks and collab flag.
- Purge cascades the three 0021 tables ahead of the operator domain, and now also
  the 0020 `ranges` rows, which it had left referencing purged provenance.
- The three files add about 26 MiB per full sync. A full `sync --server all`
  charged 523 MiB on 2026-09-29 against the old 500 MiB whole-run cap, so the
  `[sync].max_total_download_mb` default moves to 640.
- On the pinned upstream commit (`413a81a3`) the build carries 703 EN / 739 CN
  base skills, 405 EN / 421 CN operators with base skills, all 9 facilities, and
  22 EN / 25 CN collab operators. The live upstream counts in Context are
  higher; they arrive when the pin moves.

## Alternatives considered

- **Read the base data through the account client or ArkPRTS** — rejected:
  ADR 0020 (b)/(c).
- **A per-title collab filter ("Monster Hunter")** — rejected: the data names
  no collab title; a hand-curated map is not source data.
- **Import the synced base layout (`building` room assignments, morale)** —
  not asked for, and it would need an ADR 0020 roster-allowlist change.
