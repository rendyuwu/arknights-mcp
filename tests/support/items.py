"""Shared test helper: seed ``items`` rows into a candidate build.

The §T132 cost-item name pairing (§V69) resolves a module/skill upgrade cost's item
ids (``{id, count, type}``) to their display names via the ``items`` table. The
operator fixtures ship no items (item names come from the drop-rate source, imported
separately), so a test that wants a *resolved* cost name must seed the matching
``items`` rows itself. That seed logic lives here once (§V37) rather than being
copy-pasted into each operator/module tool test.

:func:`seed_items` writes the shape the T89 importer produces for one region: a penguin
``source_snapshots`` row (its own provenance chain, §V54) + one ``items`` row per
``game_id -> display_name`` entry. Opens a read-write handle because the candidate is
written before it is promoted + reopened read-only; ``penguin_statistics`` is already in
``data_sources`` from ``build_candidate`` (the full registry) so the snapshot FK holds.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path


def seed_items(path: Path, names: Mapping[str, str | None], *, region: str = "en") -> None:
    """Seed ``items`` rows (``game_id -> display_name``) into the candidate at ``path``.

    Each entry becomes one ``items`` row for ``region``; a ``None`` display name seeds an
    item present-but-unnamed (so the §T132 pairing still leaves a bare id + limitation).
    Inserts one penguin ``source_snapshots`` + ``record_provenance`` row to satisfy the
    ``items.provenance_id`` FK (§V17).
    """
    conn = sqlite3.connect(str(path))
    try:
        snapshot_id = f"item_seed:{region}"
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
            "(?, 'items', 'items', 'rh', '1', '1')",
            (snapshot_id,),
        ).lastrowid
        for game_id, display_name in names.items():
            conn.execute(
                "INSERT INTO items (server, game_id, display_name, rarity, item_type, "
                "provenance_id) VALUES (?, ?, ?, '3', 'MATERIAL', ?)",
                (region, game_id, display_name, prov),
            )
        conn.commit()
    finally:
        conn.close()
