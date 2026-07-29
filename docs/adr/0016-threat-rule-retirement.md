# ADR 0016 — Retire three threat rules that no source can feed; revive the rest from the bridge

Status: Accepted (2026-07-29, §T210 / §B160 / §V113)

Supersedes nothing. Extends the analyzer scope set by D5 (stage/enemy intelligence)
and D8 (conservative, evidence-backed advice).

## Context

The M3 rule engine (§T39) registered nine deterministic threat rules. Counted over the
promoted build `2026-07-28T170428Z-en-cn` and the pinned snapshot `413a81a3`, **four of
them had never emitted a single observation** over 3264 EN + 1200 CN stages, and a fifth
(`threat.aerial`) only ever fired one of its two arms:

| rule | observations (en) | deciding field(s) |
|---|---|---|
| `threat.lane_route` | 2453 | stage route records |
| `threat.def_res_skew` | 1598 | `def` / `res` |
| `threat.pressure_spike` | 1124 | spawn counts + timing |
| `threat.aerial` | 507 | `motion_type` (its `abilities` arm: 0) |
| `threat.tiles_deploy` | 241 | tile grid |
| `threat.ranged_arts` | **0** | `attack_type`, `attack_range` |
| `threat.block_bypass` | **0** | `block_behavior`, `abilities` |
| `threat.crowd_control` | **0** | `abilities` |
| `threat.support_aura` | **0** | `abilities` |

The cause was one layer below the rules: six allowlisted enemy columns were 100% NULL
(`enemy_levels.{attack_range,block_behavior,targeting,immunities_json,abilities_json}`
0/4343 and `enemies.attack_type` 0/3879). Nothing could see it. A rule reading an
always-NULL field takes its own §V26 "field missing → reduce confidence or record a
limitation" arm and returns **clean**, and every synthetic-fixture unit test passed
because the fixtures handed the parser already-normalized keys the §V30 bridge never
emitted. §V113 now names that class (*fetched ≠ normalized ≠ STORED*) and §B160 carries
the counts.

The emptiness had three different causes, so it needed three different answers:

1. **Bridge gap.** `normalization.py` mapped seven stats and never emitted
   `attackRange`, `targeting` or `immunities`, though upstream carries all three
   (`rangeRadius` defined on 1170/2036 EN level entries, `applyWay` MELEE 858 / RANGED
   570 / NONE 251 / ALL 99, nine typed `<x>Immune` booleans with `silenceImmune` true on
   653).
2. **Retired value domain.** `enemy_handbook.attackType` is present on every entry and
   `null` on all 1585 of them. The live home is `damageType`, and it is a LIST: 42
   enemies deal `PHYSIC` *and* `MAGIC`, so the scalar column was the wrong shape
   independently of being empty.
3. **No home at all.** `blockBehavior` and `abilities` exist in no real source.
   `attributes.blockCnt` is `m_defined:false` on 2031/2036 entries, and every
   unblockable / crowd-control / support statement upstream is either PROSE
   (`abilityList[].text`, "Cannot be blocked." ×37 — which §V26 forbids reading) or the
   free-form `skills[].prefabKey`, 626 distinct values over 1378 rows with no pinned
   vocabulary.

## Decision

**(1) and (2) are revived from their counted upstream homes.** The bridge maps
`rangeRadius`→`attack_range` (keeping upstream's `-1.0` no-radius sentinel out of a
distance column, §V103), `applyWay`→`targeting`, and the nine immunity flags into one
`immunities` list (§V67). `damageType` is imported into a new list column
(migration 0017) and `enemies.attack_type` stays, declared `retired` with its counted
evidence, so a future reader can tell "upstream went empty" from "this server forgot to
read it". `threat.ranged_arts` decides from all three, reading `targeting` **before** any
inference about reach.

**(3) is retired, not re-grounded.** `threat.block_bypass`, `threat.crowd_control` and
`threat.support_aura` are removed from the registry, along with `threat.aerial`'s
`abilities` arm and the shared `AbilityTokenRule` they were instances of.
`analyzers.rules.RETIRED_RULES` records them and why.

The rejected alternative was re-grounding the three on a classifier over
`skills[].prefabKey`. Substring matches there are non-degenerate (crowd-control-shaped
hits on 34/2036 level entries, aura 61, bypass 50), so it would have produced
observations — but the *meaning* of tokens like `Blink`, `StartRun` and `charge` is
unverified, and §V29/§V96 forbid deciding a source token's semantics by inspection.
Rules built on it would state facts nobody had checked, which is worse than stating
nothing: §V8 already gates a low-confidence finding out of a recommendation, and D8 makes
"conservative and evidence-backed" the posture. A rule with no evidence available to it
is not a conservative rule, it is an absent one.

## Consequences

* The registry is six rules, and every one owns a deciding field the real build
  populates, counted and pinned — `tests/contract/test_column_liveness.py` now has **no
  exemption list**, so a future rule added against an empty column fails on the rule
  rather than four milestones later on a zero-observation count (§V113 (b)).
* No client-visible capability is lost. The three rules emitted zero observations on
  every build ever promoted, so no response has ever contained one. `get_enemy` and
  `analyze_stage` gain `damage_types` / `targeting` / `immunities` additively (§V21), and
  a standing limitation states — with its true, corpus-wide scope (§V108) — that
  `block_behavior` and `abilities` exist for no enemy and that an analysis therefore
  never reports those three threats.
* The PRD's illustrative observation `block_bypass_risk`, whose example evidence is
  `enemy_x.ability.block_bypass=true`, describes a field the game data does not have.
  It stays as an illustration of the observation *shape*; it is not a D-decision and no
  founder decision is reversed here.
* Reviving these rules later is a data question, not a code question: it needs a deciding
  field counted non-degenerate on a real build first. The liveness guard fails in that
  direction too — if a source starts filling `blockBehavior` or `abilities`, the
  declaration flips to `live` and the test demands the rule come back.
* `FIELD_POLICY_VERSION` 11→12 and `TRANSFORM_VERSION` 5→6, so the re-import promotes
  over an unchanged snapshot (§V92).
