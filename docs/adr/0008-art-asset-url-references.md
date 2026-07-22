# ADR 0008: Art asset URL references — derive, link out, never bundle

- **Status:** Accepted
- **Date:** 2026-07-22
- **Founder decision(s):** D5 (MVP domain scope — reverses "no redistributed map
  images" *for URL references only*; image bytes stay excluded). Touches D4
  (code-only distribution) and D13 (no permission by assumption).
- **Invariants:** §V63 (new), §V16, §V1, §V24, §V5, §V19, §V27, §V21, §V28

## Context

Vision-capable LLM clients answer stage/operator/enemy questions better when
they can see the picture — a stage map above all, plus operator portraits/avatars
and enemy sprites. The request is to make those images reachable through the
server so a client can render one and, in turn, show it to the user.

The source is
[`yuanyan3060/ArknightsGameResource`](https://github.com/yuanyan3060/ArknightsGameResource),
a public mirror with `avatar/`, `portrait/`, `enemy/`, `skill/`, `item/`, `map/`,
`skin/`, and `gamedata/` folders. Its README settles the legal posture:

> "All static resources in this project are copyrighted by Arknights / Shanghai
> Yostar Network Technology Co., Ltd., for learning and exchange only. Content
> subject to removal upon request."

Two consequences: (a) the repo's **AGPL-3.0 license covers its code only** — the
images are self-declared Yostar/Hypergryph property, not open-licensed, so the
mirror grants us no rights to the underlying art (exactly the case **D13**
covers); (b) "removal upon request" means any linked URL is an **unstable base**
that can 404 without notice.

As originally written this collided with D5 ("no redistributed map images"),
principle 7 (PRD:100, avoid art), and PRD:17 ("not to republish copyrighted game
assets"). That made it a scope-and-posture decision, not a code change — the same
gate ADR 0004 and ADR 0007 apply.

## Founder decision

The repository owner (this is a personal, **private, non-commercial** project;
**no public hosting**) approves referencing `ArknightsGameResource` on
2026-07-22 under the standing takedown posture (ADR 0005 / `TAKEDOWN_POLICY.md`):
on any request from Yostar/Hypergryph or the mirror owner, references are removed
immediately. This is the D5 reversal — **for URL references only**. Image bytes
remain excluded from every release artifact and the database (§V16 unchanged).
Whether to ever host publicly stays a separate, still-blocked decision (D4
posture): if that day comes, it needs its own founder decision + legal review.

## Decision

Serve image **URL references derived at query time**, never bytes, never a
server-side fetch. The design turns on two moves that keep the hard invariants
intact by construction:

1. **Derive, don't store.** The database holds **no** URLs and **no** bytes. A
   pure service-layer function derives the URL from a game-data key we already
   hold — `operators.game_id` (a charId like `char_002_amiya`) and
   `enemies.game_id` (an enemyId like `enemy_10001_trslim`) — at response-build
   time. So §V16 stays airtight (release *and* DB remain art-free) and takedown
   is a config flip with **nothing to purge** (§V28/§V20 trivially met).
2. **Never fetch.** The server treats the derived URL as an opaque string it
   emits. It performs **no** HEAD/GET/existence check/validation — not at import,
   not at query time. A dead link is the client's to discover. Any server-side
   fetch would break §V1/§V24 and is prohibited (§V63).

Concrete shape (verified against the live repo tree, branch `main`,
2026-07-22):

- Base: `https://raw.githubusercontent.com/yuanyan3060/ArknightsGameResource/main/<folder>/<file>.png`
- **portrait** — `portrait/<game_id>_1.png` (E0 art), `_2.png` (E2 art).
- **avatar** — `avatar/<game_id>.png` (base), `_2.png` (E2).
- **enemy** — `enemy/<game_id>.png` (base; `_2` etc. are alt forms).
- **skin** — `skin/<game_id>_1b.png` (E0 full illustration), `_2b.png` (E2).
  Base skins only; alternate/paid outfits (`_epoque#4b` …) need `skin_table`,
  which we don't import → deferred.
- **Encoding:** skin variants contain `#`/`+` in filenames → any derived URL
  must percent-encode (`#`→`%23`, `+`→`%2B`). Base operator/enemy ids don't, but
  the encoder is applied unconditionally.
- **Deferred categories:** `map` → **render our own** (see below; the mirror's
  `map/` folder is 970 `act*` + 30 `a*` with **zero `main*`**, so main-story
  maps aren't linkable at all); `skill` (needs an iconId we don't store);
  alternate/paid skins (need `skin_table`); `item` (low value).

Guardrails:

- **New source ⇒ full registry entry (§V27).** `arknights_game_resource`
  (owner `yuanyan3060`, canonical URL, purpose = image references, regions,
  license/permission = *AGPL-3.0 code; assets Yostar-copyright, learning-only,
  removal-on-request*, redistribution = *reference-link only, no bytes*,
  attribution, `last_reviewed`, enabled). It rides the kill switch (§V28,
  ADR 0005): `disable` stops emitting refs. This does break the "no new source"
  economy ADR 0007 kept — an accepted cost. Note it stores no snapshot (nothing
  is imported), so `snapshot commit` is N/A for this entry.
- **Additive, optional (§V21).** An `image_refs` list of `{category, url,
  source_id}` on `get_operator` (categories `portrait`/`avatar`/`skin`) and
  `get_enemy` (`enemy`) responses (and the banner featured-op portrait where an
  `operator_pk` resolved). No breaking change.
- **No bulk surface (§V19).** Refs attach to a single already-fetched entity.
  No tool enumerates, lists, pages, or searches the art catalog.
- **Off by default, private-only.** Disabled unless explicitly enabled in
  config; cannot be turned on for any non-loopback/public deployment by a single
  flag (inherits ADR 0004 / D4). Consistent with §C private+non-commercial.
- **Region integrity (§V5).** The ref is emitted inside the entity's own region
  envelope; the game_id is already region-scoped, so en/cn never mix.
- **Zero code intake.** We reference images by URL only and copy **none** of the
  mirror's AGPL-3.0 code, so the copyleft never reaches our Apache-2.0 tree.

## Alternatives considered

- **Render our own map image from the grid data we already ingest (D5).** A
  *derived work* from structured tile/route/spawn data — no third-party art, no
  copyright dependency. This is the **chosen route for the `map` category**: the
  mirror has no main-story maps to link, and rendering covers every stage
  (main + event) uniformly. Tracked separately from the URL-ref work.
- **Store asset *keys* as facts, resolve client-side.** Emit `portrait_id` as a
  fact and let the client resolve. Weaker UX, strongest posture. Superseded here
  by query-time derivation, which stores just as little (nothing) while emitting
  a ready-to-use URL.

## Consequences

- Vision clients get operator portraits/avatars/skins and enemy sprites via
  links; no bytes or art-code ever enter our releases or DB; server-side network
  stays zero (§V1 preserved).
- The mirror is a registered, kill-switchable source; takedown = flip the flag,
  no rebuild.
- **Residual risks:** (a) underlying Yostar rights are *not* granted by the
  mirror or its AGPL license — accepted under the private/non-commercial posture
  with immediate takedown; (b) links rot silently ("removal on request") and we
  deliberately don't detect that — a client sees a broken image, never a server
  error; (c) a new legal-surface source, reversing the ADR-0007 economy; (d)
  spirit tension with PRD:17 even though bytes never touch us.
- **`map` is handled by render-own** (not linking — the mirror lacks main-story
  maps); `skill`, alternate/paid skins, and `item` stay deferred behind data we
  don't yet import (iconId, `skin_table`).
