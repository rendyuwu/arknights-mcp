# ADR 0018 — Retire the two module observations that restate their own payload

Status: Accepted (2026-07-29)

Supersedes nothing. Extends the analyzer scope set by D5 (operator/module intelligence)
and D8 (conservative, evidence-backed advice); the sibling precedent is ADR 0016, which
retired three threat rules no source could feed.

## Context

The module analyzer registered three rules. Verified live on the promoted build
`2026-07-29T083659Z-en-cn`, an operator with three modules received **nine observations,
six of which restated facts sitting beside them in the same response**:

| rule | what it emitted for Kal'tsit's PHY-X | where that already was |
|---|---|---|
| `module.stat_bonus` | `atk: +15 (Lv1->2), +10 (Lv2->3)` | nowhere — a computed delta |
| `module.trait_change` | "alters the operator's base trait at level(s) 1, 2, 3" + evidence `trait_changes.count = 1` ×3 | the `trait_changes` rows the same payload carries |
| `module.talent_change` | "adds or enhances talent(s) 1" + evidence `talent_changes.talent_index = 1` | the `talent_changes` rows, whose `talent_index` is the emitted value |

The dedup rule is explicit that evidence must not re-state numbers already in sibling facts.
Both retired rules did exactly that: their evidence pointed at a field the response
emits, with the value that field already carries. `module.trait_change`'s magnitude was a
count of a list the client can count; `module.talent_change`'s was an index the client
can read. Neither computed anything.

The distinction that keeps the third rule is not "how many rows" but **whether the
observation states something the facts do not**. `module.stat_bonus` is a cross-level
delta: the comparison rows carry each level's absolute bonus and never the step between
them, so `+15 (Lv1->2)` cannot be read off the payload without arithmetic the server has
already done. That is a finding. "This module changes talent 1" is a re-reading.

Two smaller reasons pointed the same way:

* `module.talent_change` was the only consumer of the `talentIndex == -1` reading that
  an earlier sweep falsified. Its summary said "adds or enhances **the token effect**" for any
  module carrying the sentinel — false on 454 of 513 EN rows, and stated in an
  observation rather than a fact, which is the harder surface for a client to discount.
* The retirement removes the analyzer's dependence on the change bundles entirely, so
  `ModuleLevelInput` carries stats alone. A rule that reads a bundle in future has to
  re-declare that input, which is where the real-token-set question gets asked.

## Decision

**Retire `module.trait_change` and `module.talent_change`.** The module analyzer
registers one rule, `module.stat_bonus`. `ModuleTalentChange` and the
`trait_change_count` / `talent_changes` inputs are removed rather than left unread —
an unread input is the dead substrate the liveness rule catches one domain over.

The retired rules are not re-grounded elsewhere. The facts they restated continue to
ship, unchanged and in full: `trait_changes` and `talent_changes` rows per module level,
hoisted to the module when uniform, each carrying its blackboard, its effect
template, its unlock condition, its potential rank, and — new in this change — the
`applies_to` label naming whose effect it is. Disclosure is the response's job, not a
rule's.

## Consequences

* A `compare_operator_modules(mode="with_observations")` response for a module with no
  cross-level stat movement now carries an empty `observations` list where it used to
  carry two rows. `observations` is still emitted (the key does not disappear), so this
  is not a schema change and needs no `schema_version` bump: the golden diff is
  −40 lines of observations, no field added or removed.
* The `analyzer_version` is unchanged. It versions the analyzer contract, and no
  surviving rule changed its shape; a client pinning it is not made wrong by a rule that
  stops firing, exactly as ADR 0016 concluded for the threat set.
* `tests/unit/test_module_analyzer.py` keeps one guard naming both retired `rule_id`s, so
  a reintroduction fails on the rule rather than on an observation count some future rule
  could legitimately restore.
* The reverse risk is real and accepted: a client that keyed a UI section off
  `module.talent_change` sees nothing there now. The facts it summarized are in the same
  payload under `talent_changes`, and that is the surface it should have read — the
  observation was a second, lossier copy of it.
