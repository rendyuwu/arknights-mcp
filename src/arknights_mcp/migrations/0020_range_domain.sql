-- 0020 attack-range grid domain.
-- `range_id` ("3-1", "x-4") shipped bare on every operator phase and every skill
-- level -- 2548 phase rows and 5334 skill-level rows on the promoted build -- with
-- no resolver and no limitation. An emitted opaque id has exactly two outcomes
-- (pair it with the resolution when it is imported, or emit the id plus a
-- limitation when it is not) and neither held, so "what is this skill's range" was
-- unanswerable from the response. No MCP-callable path resolved
-- the id either, so the only way to read one was to already know the game.
--
-- The registry made this worse rather than disclosing it: the primary source's
-- `fields_consumed` DECLARED `range_table.json` while no importer read it and no
-- sync fetched it -- the "fetched is not read" gap, here in its stronger
-- form (never fetched at all). The declaration implied the resolution existed.
--
-- The data IS upstream. Verified at the pinned commit 413a81a3ff3e: `range_table
-- .json` is a 68-entry (EN) / 73-entry (CN) id-keyed dict of
-- {id, direction, grids: [{row, col}]} -- 69 KiB / 75 KiB. Every one of the 56 EN /
-- 57 CN distinct `range_id` values on the promoted build resolves against its OWN
-- region's table with ZERO unresolved. CN is a superset (0-2, 1-5, 4-13, x-8, y-11);
-- the tables are region-scoped like every other imported fact, so an EN row
-- never resolves through a CN grid.
--
-- A dimension TABLE, not a denormalized column (contrast 0019's subclass_name):
-- ~68 grids are shared by 7882 referencing rows, a mean of 2 distinct ranges per
-- operator response, and the value is a LIST of up to 25 coordinates. Copying it
-- onto every phase/skill-level row would be exactly the write amplification
-- warned against, and the read is a genuine batched dimension lookup
-- (RangeRepository.by_ids, one `WHERE server = ? AND range_id IN (...)`).
--
-- grids_json holds the allowlisted [{row, col}] list as stored JSON (the
-- structure is kept whole only because every leaf is numeric -- there is no string
-- leaf in it at all, so no prose can ride in). The coordinates are integers relative
-- to the deploy tile: row/col span -3..3 and -3..6 across both regions at the pin.
--
-- `direction` is deliberately NOT imported. It is the constant 1 on all 68 EN and
-- all 73 CN entries at the pin, its semantics are unverified, and nothing would read
-- it -- an allowlisted-but-dead column is precisely what an earlier sweep caught one
-- domain over (a column must have a read path). Its constancy is COUNTED and pinned by
-- the live-upstream guard instead (constant by DATA, not by
-- construction), so an upstream entry that ever carries a second direction fails
-- loudly rather than quietly acquiring a meaning this build cannot express.
--
-- Every row carries a provenance_id FK; `server` is NOT NULL and ∈ {en,cn}.
-- `ranges` stays OUT of CRITICAL_TABLES: `range_table.json` is fetched
-- tolerant-absent (a combat-only snapshot legitimately lacks it), so an
-- empty range domain is a legitimate build. The wire then takes the OTHER arm --
-- the id plus a limitation naming what is unresolvable -- never a fabricated grid.
--
-- Identity is (server, range_id): the source is an id-keyed dict whose every entry's
-- own `id` equals its key (verified at the pin, 0 mismatches), so a duplicate is a
-- shape anomaly and collides on the constraint; the importer maps that to a typed
-- ImporterError rather than an uncaught IntegrityError tearing down the multi-region
-- build.

CREATE TABLE ranges (
    range_pk      INTEGER PRIMARY KEY AUTOINCREMENT,
    server        TEXT NOT NULL,
    range_id      TEXT NOT NULL,
    grids_json    TEXT NOT NULL,
    provenance_id INTEGER NOT NULL REFERENCES record_provenance (provenance_id),
    UNIQUE (server, range_id)
);

-- No separate index: the sole read is RangeRepository.by_ids, whose
-- `WHERE server = ? AND range_id IN (...)` is served by the UNIQUE(server, range_id)
-- constraint's own implicit index -- it filters the leading column and then the
-- second. Purge selects by provenance_id, which every domain shares. A
-- redundant index here would add write amplification and serve no query.
