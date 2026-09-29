# ADR 0012: Response-shape v0.2 (continued) — fold the M14 breaking wire reshapes into the same `schema_version` bump, then flip

- **Status:** Accepted
- **Date:** 2026-07-23
- **Founder decision:** none changed — no D1–D15 decision is reversed or refined.
  Like ADR 0011, this is a wire-contract ADR: the schema-compat rule (and
  `AGENTS.md`) forbids moving `schema_version` without an ADR, and ADR 0011
  explicitly requires that *any further* required-field reshape "return here for a
  new coordinated ADR + bump." This ADR is that record for the M14 reshapes, and
  it performs the flip. Founder-authorised the flip 2026-07-23.
- **Continues:** ADR 0011 (response-shape v0.2). This ADR does not supersede 0011;
  it widens the same, still-unreleased v0.2 revision and lands the bump 0011
  deferred.

## Context

ADR 0011 defined v0.2 as **one** coordinated `schema_version` bump for the M13
breaking wire changes (ranked-single efficiency observation, provenance hoist,
camelCase→snake_case rename). It deferred
the columnar per-level arrays out of scope, and it deferred the constant
flip itself: the flip "lands with the bundle's completion, performed by the task
that lands the last breaking change in the bundle (or a dedicated coordination
commit)." ADR 0011 also drew a hard line: *widening the breaking bundle later …
requires its own wire-contract ADR and its own coordinated `schema_version` bump.*

Two things happened after 0011:

1. **The flip never landed.** `SCHEMA_VERSION` is still `"0.1"` in
   `mcp/envelopes.py`, even though the M13 shapes (ranked observations, hoisted
   provenance, snake_case fields) already emit on the wire. The "dedicated
   coordination commit" 0011 anticipated was never made.

2. **M14 added three more breaking reshapes**, outside ADR 0011's recorded
   scope:
   - **Route digest:** `get_stage` routes collapse to
     *distinct* geometry `{start, end, checkpoints}` + `occurrence_count` (26
     records → ~4 for 4-4), WAIT-checkpoint placeholder positions are dropped, and
     the leaked camelCase checkpoint fields (`randomizeReachOffset` / `reachOffset`)
     are normalised to snake_case. A client that parsed one record per raw route,
     or read the camelCase keys, breaks.
   - **Tile-grid economy:** `get_stage` tiles change from 117
     per-tile objects to a compact per-row string grid + a symbol legend (one page
     instead of three). The additive half of the tile-grid change (the `tile_forbidden` +
     `passable:true` gloss) is safe and is **not** part of this breaking set;
     the string-grid encoding is.
   - **Per-skill template hoist:** the per-level `description`
     template (byte-identical across all ~10 skill levels, ≈30% of a full-operator
     payload) is hoisted to the parent skill; level rows carry values only. This
     task is still **`.` pending**.

So the wire today carries **five** landed breaking reshapes (three from M13, two
from M14) under a `"0.1"` tag that a true-0.1 client would not expect — the exact
fail-condition ADR 0011 named: "no shipped v0.2 client ever sees a `0.1` tag on a
reshaped payload." The tag is currently wrong, and per 0011's own rule the M14
reshapes may not be smuggled under 0.2 without this ADR.

## Decision

**Fold the M14 breaking reshapes (route digest, tile-grid string encoding,
template hoist) into the same
v0.2 revision as the M13 set, reuse the single still-unspent `0.1 → 0.2` bump,
and flip `SCHEMA_VERSION` now.**

### One bump, widened — not a second version

The v0.2 breaking set becomes:

- **M13 (ADR 0011):** ranked-single efficiency observation, provenance
  hoist, camelCase→snake_case rename.
- **M14 (this ADR):** distinct-route-geometry digest + WAIT-placeholder drop +
  checkpoint snake_case, tile-grid per-row string encoding, per-skill template hoist.

We **reuse** the `0.1 → 0.2` bump rather than mint `0.3`. Justification: the flip
never shipped externally, so **no client has ever migrated to a v0.2 that lacked
the M14 reshapes.** Folding therefore keeps the client migration count at exactly
**one** — the coordination goal 0011 states — whereas sequencing a second bump
(`0.2 → 0.3`) would cost a real migration for a version boundary no external
client ever crossed. This ADR satisfies 0011's "own ADR for further reshape"
requirement and consciously chooses *widen the unreleased version* over *add a
version*, because widening a never-released tag costs zero migrations.

### Flip now

`SCHEMA_VERSION` flips `"0.1" → "0.2"` in this coordination commit, and the tests
that pin the literal (`test_envelopes.py`, `test_serve_transport.py`, the stdio /
Streamable-HTTP smoke tests, the remote e2e) are repinned. Both transports emit
through the one shared service, so the flip is identical on `stdio` and
Streamable HTTP.

### Still deferred — unchanged

- **Columnar per-level arrays:** still deferred, still gated. It is **not**
  in the v0.2 set and this ADR does not authorise it; it remains a candidate for a
  future wire-contract ADR and a later bump.

## The template-hoist residual and the external-release gate

The template hoist is the last breaking member of the bundle and is still
pending. Because the flip lands **now**, there is a window in which the 0.2
payload's per-level skill blocks are still un-hoisted while the tag reads `"0.2"`
— the tag momentarily *leads* the template-hoist shape, the opposite of 0011's "tag trails
the shape" fail-safe direction.

This is safe, and it is the better of the two available states, for three reasons:

1. **The fail-safe is a statement about *shipped* clients.** ADR 0011's guarantee
   is that no *shipped* v0.2 client sees a `0.1` tag on a reshaped payload or a
   `0.2` tag on an un-reshaped one. **No external release occurs while the template hoist is
   pending** — this ADR gates external release on the completion of the last
   breaking member. The only boundary where the fail-safe must hold is the
   external release, and at that boundary the shape is complete and the tag is
   `0.2`. Internally, before release, the shape may still churn (0011 itself
   allows this: "there is no external release between the individual breaking
   tasks").

2. **The status quo is already wrong, un-gated.** Five breaking reshapes emit
   under `"0.1"` today. Leaving the tag at `0.1` keeps it actively lying about
   every already-landed change. Flipping now makes the tag honest about the landed
   majority and shrinks the inconsistency to exactly **one** un-emitted shape
   (the template hoist), which is release-gated. We trade a large un-gated inconsistency for a
   small gated one.

3. **The template hoist needs no second bump.** When it lands it emits its shape under the
   already-flipped `"0.2"` tag and does **not** move the constant — this ADR
   already spent the bump on its behalf. One bump, as required.

**Release gate (binding):** do not cut an external v0.2 release until the template hoist has
landed and the full breaking set emits. Until then the build is internal only.

## Consequences

- Clients migrate to the v0.2 envelope **once** for the whole M13 + M14 reshape
  (ranked observations, hoisted provenance, snake_case fields, digested routes,
  string-grid tiles, hoisted skill templates) — never a `0.2 → 0.3` chase for what
  is one logical wire revision that never shipped in between.
- The `"0.1"` tag stops advertising a shape it does not emit for the five landed
  reshapes; the residual mismatch (the template hoist) is bounded and release-gated.
- The single-bump discipline is intact and now covers M14: no task in the bundle
  moves `schema_version` on its own, additive M14 items (float precision,
  observation trim, disclaimer dedup, the tile-grid gloss) are safe and
  must **not** bump it, and any **further** breaking reshape — the deferred
  columnar arrays or anything new — still needs its own ADR and its own
  coordinated bump. This ADR does not pre-authorise them.
- The route/tile digests tighten the payload economy (fewer round trips,
  smaller payloads) while keeping the raw≠semantic discipline (distinct
  geometry + occurrence count, not raw record counts). The wire stays uniformly
  snake_case after the checkpoint-field rename.
