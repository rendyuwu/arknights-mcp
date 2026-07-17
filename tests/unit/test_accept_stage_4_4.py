"""T18: the M0 acceptance test (§V5, §V6, §V16).

This is the milestone gate for M0: it drives the *pinned 4-4 fixture* through the
entire M0 stack the way a transport would -- build candidate DB (T12) -> import
enemies (T13) + stages/levels (T14) -> call the shared ``analyze_stage`` service
(T17), which runs the deterministic threat analyzer (T16). Unlike the per-task
tests, this one asserts the whole M0 story end to end:

* **4-4 -> stage + enemy occurrence + provenance** with region on the result
  (§V5): the factual response carries ``server`` + ``snapshot_id`` +
  ``imported_at``, and ``en`` data is never surfaced under a ``cn`` query.
* **threat finding** (§V6): an evidence-backed observation with every mandated
  field (``rule_id`` + evidence + confidence + limitations + ``analyzer_version``),
  decided from a typed field (§V26).
* **no wiki text** (§V16): source-side game-content prose is stripped through the
  full pipeline -- it appears in neither the built database nor the serialized
  domain result the ``analyze_stage`` tool would return.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

from arknights_mcp.analyzers.rules.aerial import RULE_ID
from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.enemies import import_enemies
from arknights_mcp.importers.stages import import_stages
from arknights_mcp.services.stages import StageAnalysisResult, analyze_stage
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stage_4_4"
SNAPSHOT_ID = "en:fixture0000"
IMPORTED_AT = "2026-07-17T00:00:00+00:00"

#: A game-content prose sentinel: the kind of wiki/lore blurb §V16 forbids from
#: shipping. Injected only into NON-allowlisted keys, so a correct pipeline drops
#: it everywhere. If it ever surfaces in the DB or a tool result, §V16 regressed.
#: ASCII-only so JSON escaping never masks a leak (a real one would show as-is).
WIKI_PROSE = "LOREBLURB community wiki prose that must never ship to a client - see V16."


def _seed_snapshot(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "local_snapshot",
            "Local",
            "op",
            "local://x",
            "t",
            '["en"]',
            "1",
            "l",
            "p",
            "r",
            "a",
            1,
            "2026-07-17",
        ),
    )
    conn.execute(
        "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
        "manifest_hash, status, field_policy_version) VALUES (?,?,?,?,?,?,?)",
        (SNAPSHOT_ID, "local_snapshot", "en", IMPORTED_AT, "mh", "imported", "1"),
    )
    conn.commit()


def _build_from(root: Path, db_path: Path) -> sqlite3.Connection:
    """Run the full M0 build/import over a snapshot tree at ``root`` (server en)."""
    conn = build_database(db_path)
    _seed_snapshot(conn)
    adapter = LocalSnapshotAdapter(root, server="en")
    import_enemies(conn, adapter, SNAPSHOT_ID)
    import_stages(conn, adapter, SNAPSHOT_ID)
    conn.commit()
    return conn


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return _build_from(FIXTURE_ROOT, tmp_path / "cand.sqlite")


# --- 4-4 -> stage + occurrence + provenance + threat finding (§V5, §V6) --------


def test_accept_4_4_full_pipeline(conn: sqlite3.Connection) -> None:
    """The pinned 4-4 fixture yields, end to end, the stage + its enemy
    occurrences + provenance + a threat finding (the M0 acceptance story)."""
    result = analyze_stage(conn, server="en", stage_code="4-4")

    # stage + region + provenance (§V5)
    assert result.status == "ok"
    assert result.server == "en"
    assert result.stage is not None
    assert result.stage.server == "en"
    assert result.stage.game_id == "main_04-04"
    assert result.stage.stage_code == "4-4"
    assert result.stage.sanity_cost == 18
    assert result.stage.zone_game_id == "main_4"
    assert result.stage.provenance.snapshot_id == SNAPSHOT_ID
    assert result.stage.provenance.imported_at == IMPORTED_AT

    # enemy occurrence(s)
    occ_by_id = {occ.game_id: occ for occ in result.occurrences}
    assert set(occ_by_id) == {"enemy_1007_slime", "enemy_1105_drone"}
    assert occ_by_id["enemy_1105_drone"].total_count == 2
    assert occ_by_id["enemy_1105_drone"].motion_type == "FLY"
    assert occ_by_id["enemy_1007_slime"].total_count == 3

    # threat finding with every §V6 field, decided from a typed field (§V26)
    assert result.analyzer_version is not None
    assert len(result.observations) == 1
    obs = result.observations[0]
    assert obs.rule_id == RULE_ID
    assert obs.analyzer_version == result.analyzer_version
    assert 0.0 <= obs.confidence <= 1.0
    assert obs.confidence >= 0.9  # authoritative typed motion_type=FLY
    assert obs.evidence  # non-empty typed evidence
    assert isinstance(obs.limitations, tuple)
    assert {e.ref for e in obs.evidence} == {"enemy_1105_drone"}
    assert obs.evidence[0].field == "motion_type"
    assert obs.evidence[0].value == "FLY"
    assert result.warnings == ()


def test_accept_region_not_silently_mixed(conn: sqlite3.Connection) -> None:
    """§V5: the imported region is ``en``; a ``cn`` lookup of the same stage must
    not surface it (en & cn never silently mixed)."""
    assert analyze_stage(conn, server="en", stage_code="4-4").status == "ok"
    assert analyze_stage(conn, server="cn", stage_code="4-4").status == "not_found"


# --- no wiki text leaks through the whole pipeline (§V16) ----------------------


def _poison_json(path: Path, inject: dict[str, str]) -> None:
    """Add prose keys to every leaf record under the file's top-level container."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    (container,) = doc.values()  # enemyData / stages / zones
    for record in container.values():
        record.update(inject)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _poisoned_snapshot(tmp_path: Path) -> Path:
    """A copy of the pinned fixture with game-content prose injected into
    NON-allowlisted keys of the tabular records."""
    import shutil

    root = tmp_path / "poisoned" / "en"
    shutil.copytree(FIXTURE_ROOT, root)
    _poison_json(
        root / "gamedata" / "excel" / "enemy_handbook_table.json",
        {"description": WIKI_PROSE, "unlockText": WIKI_PROSE},
    )
    _poison_json(
        root / "gamedata" / "excel" / "stage_table.json",
        {"description": WIKI_PROSE},
    )
    _poison_json(
        root / "gamedata" / "excel" / "zone_table.json",
        {"lockedText": WIKI_PROSE},
    )
    return root


def test_accept_no_wiki_text_leaks_through_pipeline(tmp_path: Path) -> None:
    """§V16: even when the source snapshot carries wiki/lore prose, it survives
    into neither the built DB nor the serialized tool result."""
    root = _poisoned_snapshot(tmp_path)

    # Guard: the sentinel really is present in the source we are importing, so a
    # negative result below means the pipeline stripped it (not that it was absent).
    handbook_text = (root / "gamedata" / "excel" / "enemy_handbook_table.json").read_text(
        encoding="utf-8"
    )
    assert WIKI_PROSE in handbook_text

    conn = _build_from(root, tmp_path / "cand.sqlite")

    # The pipeline still produces the full M0 result despite the poisoned input.
    result = analyze_stage(conn, server="en", stage_code="4-4")
    assert result.status == "ok"
    assert result.stage is not None
    assert result.occurrences
    assert len(result.observations) == 1

    # §V16: prose absent from every column of the built database ...
    tables = [
        name
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    db_dump = "\n".join(
        str(row) for table in tables for row in conn.execute(f"SELECT * FROM {table}")
    )
    assert WIKI_PROSE not in db_dump

    # ... and absent from the serialized domain result a transport would return.
    result_dump = json.dumps(asdict(result), default=str)
    assert WIKI_PROSE not in result_dump


def test_accept_result_is_json_serializable(conn: sqlite3.Connection) -> None:
    """The domain result is a plain dataclass tree (no prose, no opaque objects),
    so a transport can serialize it into the typed tool envelope (T29)."""
    result = analyze_stage(conn, server="en", stage_code="4-4")
    assert isinstance(result, StageAnalysisResult)
    dumped = json.dumps(asdict(result), default=str)
    assert '"stage_code": "4-4"' in dumped
