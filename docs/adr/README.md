# Architecture Decision Records

These ADRs capture the architecture decisions that implement the
founder-approved decisions (PRD Section 18, D1–D15). Those decisions are
binding; **changing one requires a new ADR and explicit approval** (SPEC line
3; PRD Section 2).

| ADR | Title | Founder decision(s) | Invariants |
|-----|-------|---------------------|------------|
| [0001](0001-dual-transport-one-core.md) | Dual transport over one shared core | D1, D2 | §V14, §V13 |
| [0002](0002-immutable-promotion.md) | Immutable builds, atomic validated promotion | D3, D7 | §V3, §V4, §V20 |
| [0003](0003-no-query-time-source-network.md) | No query-time source network access | D3, D7 | §V1, §V2, §V24 |
| [0004](0004-code-only-distribution.md) | Code-only distribution | D4, D11 | §V16, §V17, §V19 |
| [0005](0005-source-registry-and-takedown.md) | Source registry, attribution, takedown/purge | D13, D14 | §V20, §V27, §V28 |
| [0006](0006-oauth-oidc-remote-auth.md) | OAuth/OIDC private remote; fail closed | D15, D12, D13 | §V9, §V10, §V11, §V12 |
| [0007](0007-banner-archive-carve.md) | Banner archive carve — historical FACT in, planning out | D5 | §V62, §V16, §V5 |
| [0008](0008-art-asset-url-references.md) | Art asset URL references — derive, link out, never bundle | D5 | §V63, §V16, §V1, §V27 |
| [0009](0009-image-refs-authenticated-emit.md) | Image refs on authenticated deployments (amends 0008) | D4 (refined) | §V63, §V9, §V16, §V20 |
| [0010](0010-effect-description-template-import.md) | Effect-description template import — mechanic text in, lore/story/wiki prose out | D4, D11 | §V65, §V18, §V16, §V21 |
| [0011](0011-response-shape-v0.2.md) | Response-shape v0.2 — one coordinated `schema_version` bump for the M13 breaking wire changes | none (§V21-mandated) | §V21, §V66, §V67, §V71 |
| [0012](0012-response-shape-v0.2-m14-fold.md) | Response-shape v0.2 (continued) — fold the M14 reshapes into the same bump, then flip `0.1`→`0.2` | none (§V21-mandated) | §V21, §V66, §V74, §V49 |
| [0013](0013-locale-retire.md) | Retire the extra-locale (ja/ko) NAME-alias axis — EN+CN only | founder 2026-07-23 (EN+CN only) | §V57, §V50, §V21, §V37 |
| [0014](0014-response-shape-v0.2-m16-fold.md) | Response-shape v0.2 (continued) — fold the M16 reshapes into the same unreleased bump | none (§V21-mandated) | §V66, §V22, §V21 |
| [0015](0015-skin-gallery-import.md) | Skin gallery import — named metadata in, prose and art out; alt-form via tmplId | D5-adjacent (extends 0008/0009) | §V88, §V18, §V16, §V21, §V30, §V63 |
| [0016](0016-threat-rule-retirement.md) | Retire three threat rules no source can feed; revive `ranged_arts` from the §V30 bridge | D5, D8 | §V113, §V26, §V96, §V67, §V103 |
| [0017](0017-response-shape-v0.3.md) | Response-shape v0.3 — one coordinated `schema_version` `0.2`→`0.3` for five parked breaking fixes; closes the v0.2 line | none (§V21-mandated) | §V21, §V99, §V100, §V71, §V106 |
| [0018](0018-module-observation-retirement.md) | Retire the two module observations that restate their own payload; keep the computed stat delta | D5, D8 | §V66, §V101, §V6, §V26 |
