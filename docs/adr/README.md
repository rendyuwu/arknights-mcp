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
