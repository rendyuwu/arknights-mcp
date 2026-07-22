"""``OperatorRepository.item_display_names`` batched cost-item name lookup (§T132/§V69).

The module/skill upgrade-cost name pairing resolves a cost's item ids to their
region-locale display names in ONE batched parameterized query -- a single
``WHERE game_id IN (?, …)`` with every value bound and only the ``?`` placeholder count
composed from ``len(ids)`` (§V2: structural, never a value, so injection stays
impossible). These pin the batch behaviour directly (the F2 change from an N+1 per-id
loop): a multi-id lookup returns every resolved name, an id present-but-unnamed or absent
is excluded (the caller then emits a bare id + limitation, never a fabricated name,
§V26/§V69), an empty id set short-circuits without a query, and the lookup is
region-scoped so en/cn never mix (§V5).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.support.items import seed_items

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.db.repositories.operators import OperatorRepository
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "operator" / "en"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """A candidate build seeded (en) with two named items + one present-but-unnamed."""
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    # A None display name seeds an item present-but-unnamed (§T132 leaves a bare id).
    seed_items(
        path,
        {"mat_1": "Orirock Cube", "mat_2": "Sugar", "mat_unnamed": None},
        region="en",
    )
    return open_read_only(path)


def test_batched_lookup_returns_every_resolved_name(conn: sqlite3.Connection) -> None:
    # One query for several ids -> a name for each id present with a non-null name.
    names = OperatorRepository(conn).item_display_names("en", ["mat_1", "mat_2"])
    assert names == {"mat_1": "Orirock Cube", "mat_2": "Sugar"}


def test_unnamed_and_absent_ids_are_excluded(conn: sqlite3.Connection) -> None:
    # mat_unnamed is present with a null display name; mat_absent is not in the table.
    # Neither is in the map -> the caller emits a bare id + limitation (§V26/§V69).
    names = OperatorRepository(conn).item_display_names(
        "en", ["mat_1", "mat_unnamed", "mat_absent"]
    )
    assert names == {"mat_1": "Orirock Cube"}


def test_duplicate_ids_are_deduped(conn: sqlite3.Connection) -> None:
    # The id set is deduped before binding, so a repeated id resolves once, cleanly.
    names = OperatorRepository(conn).item_display_names("en", ["mat_1", "mat_1", "mat_2"])
    assert names == {"mat_1": "Orirock Cube", "mat_2": "Sugar"}


def test_empty_id_set_short_circuits(conn: sqlite3.Connection) -> None:
    # No ids (and an all-empty-string set) -> empty map, no query needed.
    assert OperatorRepository(conn).item_display_names("en", []) == {}
    assert OperatorRepository(conn).item_display_names("en", ["", ""]) == {}


def test_lookup_is_region_scoped(conn: sqlite3.Connection) -> None:
    # §V5: the items are seeded under en, so a cn query resolves none of them -- the
    # server value is bound, en/cn are never mixed.
    assert OperatorRepository(conn).item_display_names("en", ["mat_1"]) == {"mat_1": "Orirock Cube"}
    assert OperatorRepository(conn).item_display_names("cn", ["mat_1"]) == {}
