"""T41: the M3 golden suite (§V5, §V6).

Regression-locks the deterministic, evidence-backed stage analysis end to end. It
drives the shared-core :func:`~arknights_mcp.services.stages.analyze_stage` (§V14)
over pinned snapshot fixtures built through the production candidate pipeline
(§T21/§T22), serializes the whole domain result to a canonical JSON artifact, and
compares it against a committed golden file under ``tests/golden/data/``. A change
to a rule's output, an enemy's typed stats, or the region/provenance a result
carries surfaces as a golden diff, so an unintended analysis change cannot land
silently.

``imported_at`` is pinned per build, and ``snapshot_id`` is content-derived
(``<server>:<manifest_hash[:12]>``), so provenance is fully deterministic: the
golden locks region + provenance (§V5), not only the observations (§V6).

Scenarios (all §V5/§V6):

* **4-4** -- the pinned canonical fixture -> the aerial observation;
* **drones** -- two flyers -> one aerial observation over two distinct types;
* **ranged-arts** -- an arts caster at range -> ``threat.ranged_arts``;
* **multi-route/tiles** -- two routes + a scarce deploy surface -> ``threat.lane_route``
  *and* ``threat.tiles_deploy``;
* **CN-only region separation** -- a single multi-region build where the cn stage
  is never surfaced under an ``en`` query and vice versa, and each region's
  provenance stays its own (en & cn never silently mixed, §V5).

Regenerate the golden files after an *intended* analysis change with
``UPDATE_GOLDEN=1 uv run pytest tests/golden`` and review the diff.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.services.stages import StageAnalysisResult, analyze_stage
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
GOLDEN_DIR = Path(__file__).resolve().parent / "data"
REGISTRY_PATH = REPO_ROOT / "config" / "data_sources.toml"

#: Pinned so provenance (snapshot_id + imported_at) is byte-stable across runs; the
#: golden then locks region + provenance (§V5), not only the observations (§V6).
PINNED_IMPORTED_AT = "2026-07-18T00:00:00+00:00"

#: ``UPDATE_GOLDEN=1`` rewrites the golden files instead of asserting against them.
_UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"


def _adapter(root: Path, server: str) -> LocalSnapshotAdapter:
    return LocalSnapshotAdapter(root, server, "local_snapshot")


def _build(tmp: Path, imports: list[ServerImport]) -> sqlite3.Connection:
    path = tmp / "cand.sqlite"
    build_candidate(
        path,
        imports,
        registry=load_source_registry(REGISTRY_PATH),
        imported_at=PINNED_IMPORTED_AT,
    )
    return open_read_only(path)


@pytest.fixture(scope="session")
def pinned_4_4(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    """The pinned canonical 4-4 fixture, en-only, built read-only."""
    conn = _build(
        tmp_path_factory.mktemp("golden_4_4"),
        [ServerImport("en", _adapter(FIXTURES / "stage_4_4", "en"), "local_snapshot")],
    )
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def multi_region(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlite3.Connection]:
    """One candidate holding both regions (en scenarios + a cn-only stage; §V5)."""
    conn = _build(
        tmp_path_factory.mktemp("golden_multi"),
        [
            ServerImport("en", _adapter(FIXTURES / "golden" / "en", "en"), "local_snapshot"),
            ServerImport("cn", _adapter(FIXTURES / "golden" / "cn", "cn"), "local_snapshot"),
        ],
    )
    yield conn
    conn.close()


# --- golden compare -----------------------------------------------------------


def _canonical(result: StageAnalysisResult) -> str:
    """Serialize a result to a stable JSON artifact (sorted keys, trailing NL)."""
    return json.dumps(asdict(result), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _check_golden(name: str, result: StageAnalysisResult) -> dict[str, Any]:
    """Assert ``result`` matches ``tests/golden/data/<name>.json`` (or rewrite it)."""
    text = _canonical(result)
    path = GOLDEN_DIR / f"{name}.json"
    if _UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.exists(), f"missing golden {name}.json; regenerate with UPDATE_GOLDEN=1"
    expected = path.read_text(encoding="utf-8")
    assert text == expected, f"golden drift for {name}; run UPDATE_GOLDEN=1 to refresh and review"
    return json.loads(text)  # type: ignore[no-any-return]


def _by_tag(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {o["tag"]: o for o in payload["observations"]}


def _assert_v5_v6(payload: dict[str, Any], *, server: str) -> None:
    """Every golden carries region + provenance (§V5) and well-formed obs (§V6)."""
    assert payload["status"] == "ok"
    assert payload["server"] == server
    stage = payload["stage"]
    assert stage is not None and stage["server"] == server
    prov = stage["provenance"]
    assert prov["snapshot_id"].startswith(f"{server}:")  # §V5 region on provenance
    assert prov["imported_at"] == PINNED_IMPORTED_AT
    for obs in payload["observations"]:
        # §V6: rule_id + evidence + confidence + limitations + analyzer_version.
        assert obs["rule_id"]
        assert isinstance(obs["evidence"], list) and obs["evidence"]
        assert 0.0 <= obs["confidence"] <= 1.0
        assert isinstance(obs["limitations"], list)
        assert obs["analyzer_version"] == payload["analyzer_version"]
        for ev in obs["evidence"]:
            assert ev["ref"] and ev["field"]


# --- scenarios ----------------------------------------------------------------


def test_golden_4_4(pinned_4_4: sqlite3.Connection) -> None:
    payload = _check_golden("stage_4_4", analyze_stage(pinned_4_4, server="en", stage_code="4-4"))
    _assert_v5_v6(payload, server="en")
    aerial = _by_tag(payload)["aerial"]
    assert {e["ref"] for e in aerial["evidence"]} == {"enemy_1105_drone"}
    assert aerial["confidence"] >= 0.9


def test_golden_drones(multi_region: sqlite3.Connection) -> None:
    payload = _check_golden("drones", analyze_stage(multi_region, server="en", stage_code="GS-1"))
    _assert_v5_v6(payload, server="en")
    aerial = _by_tag(payload)["aerial"]
    # Two distinct flyer types, both traced to the typed motion field (§V6/§V35).
    assert {e["ref"] for e in aerial["evidence"]} == {"enemy_drone_a", "enemy_drone_b"}
    assert "2 aerial enemy types" in aerial["summary"]


def test_golden_ranged_arts(multi_region: sqlite3.Connection) -> None:
    payload = _check_golden(
        "ranged_arts", analyze_stage(multi_region, server="en", stage_code="GS-2")
    )
    _assert_v5_v6(payload, server="en")
    arts = _by_tag(payload)["ranged_arts"]
    assert {e["ref"] for e in arts["evidence"]} == {"enemy_caster"}
    assert arts["evidence"][0]["field"] == "attack_range"


def test_golden_multi_route_tiles(multi_region: sqlite3.Connection) -> None:
    payload = _check_golden(
        "multi_route_tiles", analyze_stage(multi_region, server="en", stage_code="GS-3")
    )
    _assert_v5_v6(payload, server="en")
    # The multi-route/tiles scenario fires exactly the two stage-shape rules.
    assert {"lane_route", "tiles_deploy"} <= set(_by_tag(payload))


def test_golden_cn_region(multi_region: sqlite3.Connection) -> None:
    payload = _check_golden(
        "cn_region", analyze_stage(multi_region, server="cn", stage_code="CN-1")
    )
    _assert_v5_v6(payload, server="cn")
    assert "aerial" in _by_tag(payload)
    # Non-ASCII names survive the pipeline into the result without mojibake.
    assert payload["stage"]["display_name"] == "夜巡"


# --- §V5 region separation ----------------------------------------------------


def test_regions_never_silently_mixed(multi_region: sqlite3.Connection) -> None:
    # §V5: en data is not surfaced under a cn query and vice versa, in one DB.
    assert analyze_stage(multi_region, server="en", stage_code="CN-1").status == "not_found"
    assert analyze_stage(multi_region, server="cn", stage_code="GS-1").status == "not_found"

    cn = analyze_stage(multi_region, server="cn", stage_code="CN-1")
    en = analyze_stage(multi_region, server="en", stage_code="GS-1")
    assert cn.status == "ok" and en.status == "ok"
    assert cn.stage is not None and en.stage is not None
    assert cn.server == "cn" and cn.stage.server == "cn"
    assert en.server == "en" and en.stage.server == "en"
    # Each region's provenance stays its own -- distinct snapshots, region-tagged.
    assert cn.stage.provenance.snapshot_id.startswith("cn:")
    assert en.stage.provenance.snapshot_id.startswith("en:")
    assert cn.stage.provenance.snapshot_id != en.stage.provenance.snapshot_id
