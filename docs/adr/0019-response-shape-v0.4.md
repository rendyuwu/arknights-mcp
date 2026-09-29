# ADR 0019: Response shape v0.4 — the image-ref attribution leaves the row

- **Status:** Accepted
- **Date:** 2026-08-14
- **Continues:** the response-shape ADR line (0011 → 0012 → 0013 → 0014 → 0017). Like
  0017 this **opens a new version** rather than widening the last one; the reasoning is in
  Context.

## Context

Every `image_refs` entry carried its own copy of the registry attribution:

```json
{"category": "portrait", "path": "portrait/char_002_amiya_1.png",
 "variant": "e0", "source_id": "arknights_game_resource"}
```

`source_id` is a module constant. No derivation in `services/image_refs.py` passes it —
every construction site takes `ImageRef`'s default — so it was byte-identical on every ref
of every response, on every build. On one `get_banners` page (en page 1, `page_size=100`)
that is **576 copies of one 23-character string**, and measured through `wire_size` on the
promoted build `2026-08-13T220624Z-en-cn` it accounted for **21.4% of the entire result
frame** — both result-frame copies included.

The same response was already hoisting the *varying* half of the same family:
`image_refs_base_url` rides the payload once (ADR 0014) and each ref carries a
relative `path`. So the pattern and the home both existed; what stayed per-row was the part
that never varied.

### Why this is not a micro-optimisation

The live stake is not headroom, it is one response that was answering incompletely. At
220131 bytes that page overran the cap, so the shed step was stripping
`image_refs`, `image_refs_base_url`, `image_refs_legend` and the derived-link limitation
off it to make it fit. It was the only reachable `get_banners` window that lost its
references. Hoisting the constant takes the frame to a measured **173003 bytes (86.5% of
cap)**, so those references come back — the client gets the same rows *and* the links.

The economy is real on the other pages too (cn page 2 falls from 99.0% to 79.1% of cap, and
all nine reachable windows drop between 13.3% and 21.4% of frame), but margin is a
side-effect. The cap-shed rule buys the guarantee; this buys the answer.

### What was counted, and what the count corrected

The filing called this *"the single biggest lever on the result frame for every refs-bearing
surface"* — banners, `get_operator` skins/portraits, and search locators — and as an
**additive** change. Counted, two of those three claims were wrong:

| Surface | Claim | Counted |
|---|---|---|
| `get_banners` | biggest lever | **holds**, and bigger than filed: Δ47128 B = 21.4% of frame |
| `get_operator` | fat skin/portrait gallery | **not a lever**: 6167 refs over 884 rows ≈ 7/row; the peak row moves 0.24% of cap |
| `search_entities` / `search_stages` | ref locators | **emit no `image_refs` at all**, on any shape |
| `get_enemy` | not named in the row | 1 ref/response, so a uniform hoist **costs** 22 B |

So the change is **banners-scoped**. `get_operator` and `get_enemy` are reshaped for
uniformity, not for bytes: one key living per-row on one tool and per-response on
another would make the same key mean two things, and a client would have to know which
tool it called to know where to read the attribution. `get_enemy`'s 22-byte loss is
declared here rather than discovered later.

And the "additive" disposition was wrong. **Removing a key a client reads is breaking**
— a v0.3 client doing `ref["source_id"]` gets a `KeyError`, not a default. That is
what makes this an ADR and a `schema_version` flip instead of a footnote.

### Why this mints 0.4 instead of widening v0.3

ADRs 0011 → 0014 each folded a breaking reshape into v0.2 without touching the constant,
on the strength of one fact recorded in ADR 0012: *do not cut an external v0.2 release
until the per-skill template hoist landed*. ADR 0017 retired that licence — the hoist
landed, so the gate was discharged, and each completed version became a stable shape
rather than a moving target.
That reasoning applies one version out unchanged: v0.3 is shipped, and mutating it would
recreate exactly that condition. So v0.3 closes as shipped and this opens v0.4.

## Decision

Flip **`SCHEMA_VERSION` `"0.3"` → `"0.4"`** for one member.

- **`image_refs[].source_id` removed; `image_refs_source_id` added at the `data` level.**
  The per-ref wire shape is now `{category, path, variant}` (plus the
  additive named-gallery fields on a skin ref). The attribution rides the response
  once, beside `image_refs_base_url`.

- **One home for the hoisted set, not for each member.**
  `mcp/tools/_shared.py:image_ref_hoisted_fields()` returns all three hoisted keys, and
  `IMAGE_REFS_HOISTED_KEYS` is derived from it. `attach_image_ref_disclosures` writes that
  mapping; `mcp/tools/banners.py:_shed_refs` retires exactly those keys. Joining the
  family's shed coupler is a duty of every newly hoisted key, and a literal
  tuple at the shed site is precisely where that duty gets forgotten — a shed page still
  claiming attribution for references it had just removed.

- **Fail-closed on a ref the hoisted key cannot speak for.**
  `image_ref_to_dict` raises `ImageRefSourceError` when a ref's `source_id` is not the
  hoisted constant. `ImageRef.source_id` therefore stays on the dataclass — it is what the
  guard reads. The check is on the *value*, not on "two distinct values in one response": a
  response uniformly carrying some other mirror is equally mis-attributed by the hoisted
  constant. No live shape reaches this arm (one distinct value over every derivable ref, by
  construction), so its reachability is proven synthetically rather than
  declared — the call the `map_image` shed step got.

- **`category`/`variant` are NOT hoisted, and that is a scope decision, not an omission.**
  Both vary *within* a single row's ref list (a page carries `portrait`/`avatar` in one
  `image_refs` array, an operator adds `skin`), so collapsing them is a grouped reshape,
  not a hoist. Out of scope here.

- **The `get_banners` shed plan is kept, declared `dead_today`.** At 86.5% of cap the
  tool no longer exceeds, so the "the shedder set is exactly the over-cap set" clause would
  fail with the old boolean declaration. `cap_pressure.ShedStatus` replaces `sheds: bool`
  with `none` | `live` | `dead_today`, and the plan stays: `page_size` reaches 100 while a
  banner's `featured_ops` count is source-driven and unbounded, so the plan is reachable by
  construction and one fat event re-crosses the cap. Retiring the guarantee that bug row was filed
  for, 13.5 points from the cap, would trade a rule for an economy — the filing's own note says
  ship both.

## Consequences

- **A v0.3 client must move.** `ref["source_id"]` is gone; read `data.image_refs_source_id`
  once per response. Nothing else in the envelope changes.
- **No rebuild, no promote.** This is emit-side only: `FIELD_POLICY_VERSION` and
  TRANSFORM_VERSION` are untouched, no stored byte and no URL moves, and derive-don't-store
  is intact — so no promote is owed and the promoted build stays valid.
- **One `get_banners` window gains data rather than losing it.** en page 1 at
  `page_size=100` returns its `image_refs` again instead of having them shed.
- `get_enemy` responses grow by 22 bytes. Declared, accepted, and pinned by a test so it
  cannot be re-litigated as a regression later.
- **The recurrence guard, filed with this change:** a per-row constant that is
  invariant by construction belongs in one response-level home, its invariance counted and
  fail-closed, and the hoisted key joins its family's shed coupler. The neighbouring
  clauses all missed this class — the description and observation dedup rules target
  tool-description and observation strings, and the payload-dedup clauses cover
  per-effect/per-observation/per-level payloads. None of them look at a per-row payload
  constant.
- The v0.3 line (ADR 0017) closes as shipped. A further breaking reshape opens v0.5 under
  its own tracked task and ADR; it does not widen v0.4.
