# ADR 0010: Effect-description template import — mechanic text in, lore/story/wiki prose stays out

- **Status:** Accepted
- **Date:** 2026-07-22
- **Founder decision:** D4, D11 (code-only distribution; excluded game-content classes)
- **Invariants:** §V65, §V18, §V16, §V21, §V37, §V14

## Context

§V65 records a design defect (B56): a skill/talent/module response that emits
only blackboard key-value data with **no effect text** forces a client LLM to
invent mechanics from key names alone. Some keys are famously counterintuitive
(Spirit Burst `stun:10` is a *self*-stun; `amiya_t_1[atk].sp` is unknowable from
the key), so the bare-blackboard design forces the exact fabrication the server
instructions forbid.

§V65 defines three grounding paths, at least one required on every effect emit:

- **(a)** import the in-game effect-description **TEMPLATE** and emit it
  alongside the blackboard — the template references the blackboard keys, so
  template + values = grounded prose;
- **(b)** a standing limitation ("effect text not included; don't infer
  mechanics from key names");
- **(c)** a common-key glossary in the tool description.

T126 shipped the floor (b)+(c). T127 is path (a) — **the fix** — and §V65 makes
it explicitly ADR-gated: *"mechanic text ⊥ story/wiki prose — §C/§V16 ceiling
check via ADR."* This ADR is that ceiling check.

The gate is §V16, which bans a release artifact or runtime store from carrying
"artwork, audio, **story script**, voice line, **wiki/community prose**, or full
announcement body." The question is whether an in-game effect-description
template falls inside that forbidden set.

The verified upstream shapes settle the distinction. Two **different** kinds of
`description`-named field live in the operator/skill/module tables:

- **Mechanic templates** — `skill_table` `levels[].description`,
  `character_table` `talents[].candidates[].description`, and the
  `battle_equip_table` part bundles' `additionalDescription` / `overrideDescripton`
  (trait) and `upgradeDescription` / `description` (talent). Each is short combat
  text that references the sibling blackboard via placeholders
  (`<@ba.vup>{atk_scale:0%}</>`, `stuns for {stun} seconds`). It describes what
  the effect does mechanically; it is meaningless without the numeric blackboard
  it annotates.
- **Lore prose** — `character_table.description` (the operator's top-level
  personality/story blurb) and `uniequip_table` `uniEquipDesc` (the module's
  flavor blurb). These are narrative text with no blackboard reference.

## Decision

Carve **mechanic effect-description templates** into the field allowlist; keep
**lore / story / voice / wiki-community prose** excluded.

- **In:** the per-level skill effect template (`skill_table` `levels[].description`),
  the talent-candidate effect template (`character_table`
  `talents[].candidates[].description`), and the module trait/talent-change
  effect templates (`battle_equip_table` part bundles). Each is added to its
  record's explicit allowlist, capped + control-stripped + sanitized as untrusted
  data (§V18), and emitted **alongside** its blackboard so template + values are
  self-grounding (§V65 path (a)). The allowlist widening bumps
  `FIELD_POLICY_VERSION` (5 → 6), stamped on every snapshot + provenance row.
- **Out (ceiling holds, §V16):** the operator lore blurb
  (`character_table.description`), the module lore blurb (`uniEquipDesc`), and all
  story scripts, voice lines, wiki/community prose, and announcement bodies. None
  is allowlisted; none is stored. The mechanic templates are game-data combat
  text, not narrative prose — importing them does **not** widen §V16, it clarifies
  that mechanic text was never in the forbidden set. The lore/story/wiki classes
  §V16 names remain permanently excluded and are unchanged by this ADR.
- **Storage:** the `skill_levels` / `talent_levels` / `module_levels`
  `gameplay_description` columns already exist (migration 0004, provisioned
  "policy-controlled … excluded by default unless field policy permits"). T127 is
  the moment field policy permits populating skill/talent templates there; the
  module trait/talent templates ride the existing per-candidate
  `trait_changes_json` / `talent_changes_json` so the template sits next to the
  blackboard it grounds. **No new migration** — the storage was provisioned for
  exactly this.
- **Emit shape:** the template rides an **additive, optional** `description` field
  on each skill level / talent variant / module trait/talent change (§V21). No
  required field changes and no `schema_version` bump; a client that ignores it is
  unaffected. Both transports emit it through the one shared service (§V14). The
  §V65 (b) standing limitation stays (reworded: template included when available,
  may be absent for some effects, blackboard keys still raw) so every effect emit
  keeps ≥1 grounding path even when a specific template is absent.

Widening beyond mechanic templates — importing lore, story, voice, or
wiki/community prose — remains forbidden by §V16 and would require a new founder
decision and a new ADR, never a config flag (same posture as ADR 0004's
distribution gate).

## Consequences

- A client can read the effect-description template alongside the blackboard and
  ground each key's meaning in context, instead of guessing from key names — the
  fabrication B56 documents is fixed at the source, not just disclaimed.
- The §V16 ceiling is intact and made sharper: mechanic templates are in, the
  lore/story/voice/wiki-prose classes stay permanently out, and the boundary is
  pinned by this ADR + §V65.
- The change is additive (§V21): existing consumers are unaffected, no
  `schema_version` bump, no breaking rename. `FIELD_POLICY_VERSION` records the
  allowlist widening on every snapshot.
- Templates are untrusted imported strings like every other field: control-char
  stripped, length-capped, never concatenated into server instructions or tool
  descriptions (§V18).
- No new data source and no legal-posture change — the templates ride the
  existing primary-snapshot registry entry, so `get_data_sources` is unchanged.
