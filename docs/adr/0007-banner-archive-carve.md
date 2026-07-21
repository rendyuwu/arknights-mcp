# ADR 0007: Banner archive carve — historical FACT in, planning stays out

- **Status:** Accepted
- **Date:** 2026-07-21
- **Founder decision:** D5 (MVP domain scope — banners deferred)
- **Invariants:** §V62, §V16, §V17, §V5, §V30, §V26, §V7

## Context

D5 approved the v0.1 MVP domain scope as stage/enemy plus operator/module
intelligence and **deferred banners, farming, and roster-aware planning**. PRD
Section 6.1 restates the exclusion of "banner prediction, gacha planning, and
shop forecasting"; roadmap Section 6.3 lists "banner and event history
snapshots" and, separately, "pull probability and spark planning" as distinct
Phase 2 candidates.

Two of those Phase 2 candidates have already been carved in under the same
pattern this ADR follows: the Penguin Statistics drop-rate cache (§V52–V55) and
the official-announcement metadata adapter (§V56, ADR 0005 D14 gate). Each took
one narrowly scoped **historical FACT** slice of a deferred domain, left the
**planning/prediction** slice deferred, and pinned the boundary with an
invariant. Banners are the third.

The deferred "banners" line in D5 actually spans two very different things:

- a **historical schedule FACT** — which gacha pools ran, when they opened and
  closed, their rule type, and the featured operator; and
- **gacha PLANNING** — pull-probability, spark, rate-pity, and shop forecasting.

The verified shape of the primary source settles which of these is even
buildable. `gamedata/excel/gacha_table.json` `gachaPoolClient` (389 EN entries,
past plus near-future scheduled) carries `gachaPoolId`, `gachaPoolName`,
`openTime`/`endTime` (clean unix-epoch integers), `gachaRuleType`, and a
**typed** featured-operator reference for two rule-type families only:
`LIMITED` → `limitParam.limitedCharId`, and the CLASSIC family
(`ATTAIN`/`CLASSIC`/`CLASSIC_ATTAIN`/`CLASSIC_DOUBLE`/`FESCLASSIC`/`SPECIAL`) →
`dynMeta.attainRare6CharList`. The standard-banner rule types
(`NORMAL`/`SINGLE`/`DOUBLE`/`LINKAGE`, 282/389 EN) carry **no** typed
featured-op; the rate-up lives only in prose (CN `<@ga.up>` HTML,
EN `gachaPoolDetail` = `"-"`), which §V18 forbids importing. Critically, the
rate/pity/spark constants that planning would require are **verified absent**
from `gacha_table` — building a planner would mean hardcoding non-snapshot
constants, exactly the kind of unsourced fabrication the project's FACT/observation
discipline exists to prevent.

## Decision

Carve the **banner ARCHIVE** (historical schedule FACT) into scope; keep
**gacha PLANNING** (pull-probability, spark, rate-pity, shop forecasting)
deferred.

- The archive is a **FACT domain**: each pool carries region and provenance
  (§V5/§V17) and is metadata-only (§V62/§V16 ceiling) — pool id, display name
  (capped + sanitized §V18), open/end time (epoch → ISO), rule type, and the
  **typed** featured-op per rule type. No `gachaPoolSummary`,
  `gachaPoolDetail`, `dynMeta` prose, HTML, or image is stored. Standard-rule
  banners emit no featured-op plus a limitation "standard-banner rate-up not in
  typed gamedata" (§V26 missing-field → limitation; never fabricate or parse
  prose §V7).
- The archive reuses the **primary `arknights_assets_gamedata` snapshot** — the
  same snapshot as enemy/stage/operator data. It introduces **no new source,
  adapter, or registry entry**; `gacha_table.json` is fetched every sync via
  `SUPPLEMENTARY_FILES` (tolerant-absent, §V41/B36), and the §V41 introspection
  test catches an un-wired file.
- Featured char ids **soft-resolve** to an `operator_pk` when the operator is
  present, else carry the raw char id plus a limitation; an unresolvable
  featured-op never fails the build (archive is a standalone FACT, §V3
  preserved; operators are optional-zero for a combat-only snapshot, B36). A
  non-empty `gacha_table` yielding zero banners fails closed (§V30).

Planning stays **out and ADR-gated**: no rate/pity/spark/shop model ships,
because the constants are verified absent from the snapshot (§C). Widening the
archive into planning requires a new founder decision, a new source of those
constants, and a new ADR — not a config flag (same posture as ADR 0004's
public-distribution gate).

## Consequences

- Users can query historical banner schedules and typed featured operators with
  region and provenance, without any prediction surface.
- The metadata-only ceiling (§V62/§V16) is permanent; standard-banner rate-up is
  reported as a limitation, never inferred from prose.
- No new data source or legal-posture change: the archive rides the existing
  primary-snapshot registry entry, so `get_data_sources` is unchanged.
- A future planning capability remains a deliberate, gated decision rather than
  an incremental slide — the FACT/planning boundary is pinned by §V62 and this
  ADR.
