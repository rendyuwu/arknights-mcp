"""Shared test helper: seed a penguin drop-rate cache into a candidate build.

Both the ``get_stage_drops`` tool tests (§T91) and the MCP Inspector contract need
a penguin ``stage_drops`` row so the drop tool has a live target; the seed logic
lives here once (§V37) rather than being copy-pasted into each. It writes the shape
the T89 importer produces: a penguin ``source_snapshots`` row (its OWN provenance
chain, distinct from the game-data fact, §V54) + an ``items`` row + one
``stage_drops`` row stamped with ``fetched_at`` / ``expires_at`` (§V53), so the
caller controls the fresh/stale verdict deterministically.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: Deterministic expiry stamps: 2099 is always in the future, 2020 always past, so
#: a §V53 fresh/stale verdict holds regardless of the real clock at test time.
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"
PAST_EXPIRY = "2020-01-01T00:00:00+00:00"


def seed_stage_drop(
    path: Path,
    *,
    expires_at: str = FUTURE_EXPIRY,
    stage_code: str = "4-4",
    region: str = "en",
    item_game_id: str = "sugar",
    item_display_name: str | None = "Sugar",
    drop_rate: float | None = 0.25,
    times: int | None = 5000,
) -> None:
    """Seed one penguin ``(stage, item)`` drop into the candidate at ``path``.

    Inserts a penguin snapshot + item + ``stage_drops`` row for the stage keyed by
    ``(region, stage_code)``, stamped with ``expires_at`` so the caller controls the
    §V53 fresh/stale verdict. Opens a read-write handle (the candidate is written
    before it is promoted + reopened read-only), mirroring the T89 importer's shape;
    ``penguin_statistics`` is already seeded into ``data_sources`` by
    ``build_candidate`` (the full registry, so the snapshot FK holds).
    """
    conn = sqlite3.connect(str(path))
    try:
        stage_pk = conn.execute(
            "SELECT stage_pk FROM stages WHERE server = ? AND stage_code = ?",
            (region, stage_code),
        ).fetchone()[0]
        snapshot_id = f"pg:{region}"
        conn.execute(
            "INSERT INTO source_snapshots (snapshot_id, source_id, server, fetched_at, "
            "imported_at, manifest_hash, status, field_policy_version) VALUES "
            "(?, 'penguin_statistics', ?, '2026-07-19T00:00:00+00:00', "
            "'2026-07-19T00:00:00+00:00', 'ph', 'imported', '1')",
            (snapshot_id, region),
        )
        prov = conn.execute(
            "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
            "record_hash, transform_version, field_policy_version) VALUES "
            "(?, 'result/matrix', ?, 'rh', '1', '1')",
            (snapshot_id, f"{stage_code}/{item_game_id}"),
        ).lastrowid
        item_pk = conn.execute(
            "INSERT INTO items (server, game_id, display_name, rarity, item_type, provenance_id) "
            "VALUES (?, ?, ?, '3', 'MATERIAL', ?)",
            (region, item_game_id, item_display_name, prov),
        ).lastrowid
        quantity = None if drop_rate is None else int(round(drop_rate * (times or 0)))
        conn.execute(
            "INSERT INTO stage_drops (stage_pk, item_pk, region, quantity, times, drop_rate, "
            "snapshot_id, fetched_at, expires_at, provenance_id) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, '2026-07-19T00:00:00+00:00', ?, ?)",
            (
                stage_pk,
                item_pk,
                region,
                quantity,
                times,
                drop_rate,
                snapshot_id,
                expires_at,
                prov,
            ),
        )
        conn.commit()
    finally:
        conn.close()
