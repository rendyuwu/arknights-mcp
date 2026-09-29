# ADR 0009: Image refs on authenticated deployments — private = access-controlled, not loopback-only

- **Status:** Accepted
- **Date:** 2026-07-22
- **Amended:** the founder (2026-07-22) — the flag default was flipped OFF→ON so all
  existing user features are default-enabled. See [Amendment](#amendment--on-by-default-founder-2026-07-22)
  below. Derive-not-store, never-fetch, startup fail-closed, and the kill
  switches are unchanged.
- **Amends:** ADR 0008 (art asset URL references). This refines, does not
  reverse, that decision: derive-not-store and never-fetch stay intact; only the
  *deployment posture* under which a ref may be emitted changes.
- **Founder decision(s):** D4 (code-only distribution / public-exposure posture)
  — refined. Still touches D5, D13 exactly as ADR 0008 left them.

## Context

ADR 0008 shipped the `image_refs` surface with a deliberately conservative
posture gate: emit only when

```
image_refs_enabled = [image_refs].enabled AND not mcp.remote.requires_auth
```

`requires_auth` is true for any non-loopback bind, or any loopback bind declared
`behind_proxy`. So the surface could turn on **only on a genuine
loopback dev bind** — a single flag could never expose it on a
proxied/non-loopback deployment. ADR 0008 said as much: "Whether to ever host
publicly stays a separate, still-blocked decision (D4 posture) … it needs its own
founder decision + legal review." This is that decision.

The deployment in question is **not open**: a Cloudflare tunnel fronts a loopback
bind (`behind_proxy=true`), and Auth0 OIDC gates every request with a required
scope `arknights:read`. Every viewer is authenticated. The refs themselves remain
**derived third-party GitHub URLs** (`yuanyan3060/ArknightsGameResource`) — no art
bytes ever enter a release or the DB, and the server still never
fetches them.

The key realization: the old gate conflated **"private"** with **"loopback-only,"**
but the actual intent of D4 is **access control**. And the startup gate
(`enforce_remote_posture`) already **fails closed** — it raises `ConfigError`
whenever `requires_auth` is true without HTTPS + valid OIDC. So the set of
*startable* deployment postures is exactly two:

1. `requires_auth` true → HTTPS + OIDC enforced (authenticated), or startup aborts;
2. `requires_auth` false → a genuine loopback dev bind (authless, owner's machine).

There is **no startable anonymous non-loopback surface** to protect against. The
`not requires_auth` term therefore meant "loopback-only," which is *stricter* than
"private/access-controlled" — not a security boundary, just a narrower one than
intended.

## Decision

Redefine "private" for the `image_refs` gate as **access-controlled**, not
loopback-only. Concretely, **drop the deployment-posture term**:

```
# was
image_refs_enabled = [image_refs].enabled AND not mcp.remote.requires_auth
# now
image_refs_enabled = [image_refs].enabled
```

The registry `enabled` check for `arknights_game_resource` (the kill switch)
stays a separate, additional gate at the wiring layer, unchanged. So a ref is
emitted iff:

- `[image_refs].enabled` is set (ON by default; see Amendment), **and**
- the `arknights_game_resource` source is enabled in the machine registry.

Why the posture term is safe to remove rather than flip to `OR`:

- `requires_auth OR is_loopback` is a tautology (`requires_auth` is
  `not is_loopback or behind_proxy`), so flipping the term to an OR is equivalent
  to removing it — the honest form is removal.
- The startup gate `enforce_remote_posture` guarantees every *remote* startable surface is
  OIDC-authenticated; the only authless startable surface is loopback dev, which
  is the owner's own machine. Both are access-controlled in the sense D4 cares
  about.

## What does NOT change

- **Derive, don't store.** No URL and no byte is persisted. Takedown
  stays a config flip with nothing to purge.
- **Never fetch.** The derived URL is an opaque emit string; no
  HEAD/GET/existence-check, at import or query time.
- **Registry kill switch.** `source disable arknights_game_resource` stops
  every ref immediately.
- **Two kill switches.** Both off switches remain: set `[image_refs].enabled`
  false, or disable the `arknights_game_resource` source. (The flag *defaults*
  ON — see the Amendment — but either switch still fully suppresses the surface.)
- **Standing takedown posture (ADR 0005 / `TAKEDOWN_POLICY.md`).** On any request
  from Yostar/Hypergryph or the mirror owner, references are removed immediately.
- **Region integrity, no bulk surface, zero code intake.**

## Consequences

- The authenticated (Auth0-gated) deployment can now serve `image_refs` when the
  operator opts in — the intended use case that motivated ADR 0008.
- The surface is still unreachable on an **open/anonymous** deployment, because
  no such deployment can start (startup fails closed). "No open public hosting"
  survives; only "no *authenticated* public" is lifted.
- **Residual risks (unchanged from ADR 0008):** underlying Yostar rights are not
  granted by the mirror; links rot silently under "removal on request"; spirit
  tension with PRD:17 even though bytes never touch us. All accepted under the
  private, non-commercial, immediate-takedown posture — now read as
  *access-controlled* rather than *loopback-only*.

## Alternatives considered

- **Keep loopback-only, run a separate local instance for vision clients.**
  Rejected: forces a parallel deployment for the exact private, authenticated use
  case the owner already runs; no added protection given the startup gate.
- **Flip the term to `requires_auth OR is_loopback`.** Equivalent to removal (a
  tautology) but hides that fact behind dead logic; removal is clearer and
  matches the real invariant.

## Amendment — ON by default (founder 2026-07-22)

The founder's follow-through on this decision: since every *startable* deployment is
access-controlled, and this project's own deployment is the intended
authenticated, private, non-commercial use case, the conservative OFF default is no
longer earning its friction. Flip `[image_refs].enabled` from `false` to `true` so all
existing user features are default-enabled — a fresh install carries `image_refs` on
`get_operator`/`get_enemy` with no config edit. The `arknights_game_resource` source is
likewise shipped `enabled=true` in the machine registry.

Nothing about the *shape* of the gate changes, only its default:

- **Emission gate unchanged.** A ref is still emitted iff `[image_refs].enabled` **and**
  the `arknights_game_resource` source is registry-enabled. Both now simply default on.
- **Kill switch unchanged.** Set `[image_refs].enabled = false` **or**
  `arknights-mcp source disable arknights_game_resource` and every ref stops immediately —
  with nothing to purge.
- **Startup fail-closed unchanged.** There is still no startable open/anonymous public
  surface; the default flip cannot expose refs on a deployment that cannot start.
- **Derive-not-store / never-fetch unchanged.** No URL or byte is
  persisted and the server never fetches a derived link.

`kengxxiao_gamedata` stays CI-only and is **not** a runtime feature.
