"""T92: the M8 acceptance test (§V5, §V16, §V53, §V55).

The milestone gate for M8 (penguin drop-rate intelligence). It drives a *pinned
penguin snapshot fixture* (``tests/fixtures/penguin/snapshot.json`` -- small, no
bulk data, §V16) through the entire M8 stack the way a CLI sync would, then reads
it back through the shared-core service both transports call (§V14):

  build multi-region candidate (golden en+cn fixtures) -> import_penguin_drops
  (T89) for US->en + CN->cn against a fixture fetcher (never a live fetch, §V52)
  -> reopen read-only -> get_stage_drops (T91) with an injected clock.

Unlike the per-task unit tests (which seed a single drop row directly), this one
asserts the whole M8 story end to end:

* **drops + region + penguin provenance** (§V5/§V54): each drop carries its region
  plus the penguin ``snapshot_id`` + ``fetched_at`` + ``expires_at`` chain (its OWN
  provenance, distinct from the game-data fact), and en drops are never surfaced
  under a cn query (en & cn never silently mixed).
* **fail-closed skip** (§V30): a matrix row for an absent stage or item is skipped,
  never fabricated.
* **no prose leaks** (§V16/§V18): drop-item source prose is stripped through the
  full pipeline -- absent from both the built DB and the served result.
* **expiry -> data_stale** (§V53): a drop past its ``expires_at`` flips the status
  to ``data_stale`` and is flagged ``expired`` while still returned, never presented
  as fresh.
* **farming efficiency w/ confidence + limitation** (§V55/§V6/§V8/§V7): the
  observations carry every §V6 field; a thin sample and an expired cache each drop
  confidence below the §V8 recommendation threshold with a limitation; no
  prescriptive "best-farm"/"mandatory" verdict.
* **attribution surfaced** (§V53/§V27): ``get_data_sources`` reports the penguin
  source's CC BY-NC 4.0 license + attribution + ``last_reviewed`` date, with the
  imported en/cn drop snapshots enriched as active snapshots.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.penguin_drops import import_penguin_drops
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.services.drops import StageDropsResult, get_stage_drops
from arknights_mcp.services.source_status import get_data_sources
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN = REPO_ROOT / "tests" / "fixtures" / "golden"
PENGUIN_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "penguin" / "snapshot.json"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: Pinned so the game-data provenance (snapshot_id + imported_at) is byte-stable.
PINNED_IMPORTED_AT = "2026-07-18T00:00:00+00:00"

#: Pinned penguin fetch time; default 7-day TTL -> expires_at = 2026-07-08. The two
#: clocks below straddle that expiry so the §V53 fresh/stale verdict is deterministic
#: (no wall-clock coupling), regardless of the real time the suite runs.
PINNED_FETCHED = datetime(2026, 7, 1, tzinfo=UTC)
NOW_FRESH = datetime(2026, 7, 5, tzinfo=UTC)
NOW_EXPIRED = datetime(2026, 7, 20, tzinfo=UTC)

#: A drop-item prose sentinel: the kind of source blurb §V16/§V18 forbids from
#: shipping. It rides a NON-allowlisted item key in the fixture, so a correct
#: pipeline drops it. ASCII-only so JSON escaping never masks a leak.
PENGUIN_PROSE = "PENGUINPROSE"

#: Prescriptive language the farming analyzer must never emit (§V7/§V55).
_PROSCRIBED = ("best farm", "best-farm", "mandatory", "must ", "should ", "always farm")


class _FixtureFetcher:
    """Serves the pinned snapshot per (server, endpoint); never touches the network.

    Mirrors :meth:`PenguinStatsAdapter.fetch` so the real T89 importer runs against
    it unchanged (§V52: penguin is fetched only at CLI sync/import, never a
    query-time call; here even the "fetch" is a pinned fixture read).
    """

    def __init__(self, data: dict[str, dict[str, object]]) -> None:
        self._data = data

    def fetch(self, endpoint: str, *, server: str | None = None) -> object:
        assert server is not None
        return self._data[server][endpoint]


def _adapter(server: str) -> LocalSnapshotAdapter:
    return LocalSnapshotAdapter(GOLDEN / server, server, "local_snapshot")


def _build(tmp_path: Path) -> Path:
    """Build the multi-region golden candidate, then import the penguin drops.

    The candidate is written writable (as a CLI sync would, before promotion +
    read-only reopen); the penguin importer runs over the same file, then the
    caller reopens it read-only for the query path (§V2).
    """
    path = tmp_path / "cand.sqlite"
    build_candidate(
        path,
        [
            ServerImport("en", _adapter("en"), "local_snapshot"),
            ServerImport("cn", _adapter("cn"), "local_snapshot"),
        ],
        registry=load_source_registry(REGISTRY),
        imported_at=PINNED_IMPORTED_AT,
    )
    fetcher = _FixtureFetcher(json.loads(PENGUIN_FIXTURE.read_text(encoding="utf-8")))
    conn = sqlite3.connect(str(path))
    try:
        us = import_penguin_drops(conn, fetcher, penguin_server="US", fetched_at=PINNED_FETCHED)
        cn = import_penguin_drops(conn, fetcher, penguin_server="CN", fetched_at=PINNED_FETCHED)
        # US -> en resolves gs_drones/{30012,30013} + gs_arts/30012 (3), skips the
        # absent stage + absent item (2, §V30 fail-closed); CN -> cn resolves cs_1/30012.
        assert (us.region, us.drops_inserted, us.drops_skipped) == ("en", 3, 2)
        assert (cn.region, cn.drops_inserted, cn.drops_skipped) == ("cn", 1, 0)
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return open_read_only(_build(tmp_path))


# --- drops + region + penguin provenance (§V5/§V54) ---------------------------


def test_accept_fresh_drops_carry_region_and_penguin_provenance(conn: sqlite3.Connection) -> None:
    result = get_stage_drops(conn, server="en", stage_code="GS-1", now=NOW_FRESH)
    assert result.status == "ok"
    assert result.stale is False
    assert result.server == "en"
    assert result.stage is not None and result.stage.server == "en"
    # §V5: the stage's game-data provenance is region-tagged + pinned.
    assert result.stage.provenance.snapshot_id.startswith("en:")
    assert result.stage.provenance.imported_at == PINNED_IMPORTED_AT

    drops = {d.item_game_id: d for d in result.drops}
    assert set(drops) == {"30012", "30013"}  # ordered by item game_id in the repo
    sugar = drops["30012"]
    assert sugar.region == "en"
    assert sugar.quantity == 1250 and sugar.times == 5000
    assert sugar.drop_rate == 0.25
    # §V54: the drop's OWN penguin provenance chain (distinct from the game-data fact).
    assert sugar.snapshot_id.startswith("en:")
    assert sugar.snapshot_id != result.stage.provenance.snapshot_id
    assert sugar.fetched_at == PINNED_FETCHED.isoformat()
    assert sugar.expires_at and sugar.expired is False


def test_accept_result_is_json_serializable_and_prose_free(conn: sqlite3.Connection) -> None:
    # A plain dataclass tree a transport can serialize into the typed envelope (T29),
    # and §V16/§V18: source drop-item prose survives into neither the DB nor the result.
    result = get_stage_drops(conn, server="en", stage_code="GS-1", now=NOW_FRESH)
    assert isinstance(result, StageDropsResult)
    dumped = json.dumps(asdict(result), default=str)
    assert '"drop_rate": 0.25' in dumped
    assert PENGUIN_PROSE not in dumped

    tables = [
        name
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    db_dump = "\n".join(
        str(row) for table in tables for row in conn.execute(f"SELECT * FROM {table}")
    )
    assert PENGUIN_PROSE not in db_dump


# --- §V53 expiry -> data_stale ------------------------------------------------


def test_accept_expired_cache_is_data_stale_but_returned(conn: sqlite3.Connection) -> None:
    result = get_stage_drops(conn, server="en", stage_code="GS-1", now=NOW_EXPIRED)
    assert result.status == "data_stale"
    assert result.stale is True
    # The drop is flagged, not withheld (§V53): the payload still carries it.
    assert result.drops and all(d.expired for d in result.drops)
    # §V5: region + provenance still ride a stale-but-delivered fact.
    assert result.stage is not None and result.stage.server == "en"


# --- §V55 farming efficiency: confidence + limitation -------------------------


def test_accept_efficiency_confidence_and_limitation_fresh(conn: sqlite3.Connection) -> None:
    result = get_stage_drops(
        conn, server="en", stage_code="GS-1", include_efficiency=True, now=NOW_FRESH
    )
    assert result.status == "ok"
    assert result.analyzer_version is not None
    obs = {o.evidence[0].ref: o for o in result.observations}
    assert set(obs) == {"30012", "30013"}

    for ob in result.observations:
        # §V6: every observation carries the five mandated fields, decided from
        # typed numeric fields only (§V26).
        assert ob.rule_id == "farming.sanity_per_item"
        assert ob.analyzer_version == result.analyzer_version
        assert 0.0 <= ob.confidence <= 1.0
        assert isinstance(ob.limitations, tuple)
        assert {e.field for e in ob.evidence} >= {"sanity_cost", "drop_rate", "sanity_per_item"}
        # §V7/§V55: facts + observations only, never a prescriptive verdict.
        blob = (ob.summary + " " + " ".join(ob.limitations)).lower()
        assert not any(word in blob for word in _PROSCRIBED)

    # A well-sampled drop (times=5000) is a stable figure; a thin sample (times=40)
    # is downgraded below the §V8 recommendation threshold with a limitation (§V55).
    stable, thin = obs["30012"], obs["30013"]
    assert stable.confidence >= 0.5 and stable.limitations == ()
    assert thin.confidence < 0.5
    assert any("floor" in lim.lower() or "noisy" in lim.lower() for lim in thin.limitations)


def test_accept_expired_efficiency_downgraded_below_recommendation(
    conn: sqlite3.Connection,
) -> None:
    # §V53/§V55: an expired cache downgrades every figure below the §V8 threshold, so
    # it reads as a limitation, never a fresh recommendation.
    result = get_stage_drops(
        conn, server="en", stage_code="GS-1", include_efficiency=True, now=NOW_EXPIRED
    )
    assert result.status == "data_stale"
    assert result.observations
    for ob in result.observations:
        assert ob.confidence < 0.5
        assert any("expired" in lim.lower() for lim in ob.limitations)


# --- §V5 region separation ----------------------------------------------------


def test_accept_regions_never_silently_mixed(conn: sqlite3.Connection) -> None:
    # §V5: en drops are not surfaced under a cn query and vice versa, in one DB.
    cn_gs1 = get_stage_drops(conn, server="cn", stage_code="GS-1", now=NOW_FRESH)
    en_cn1 = get_stage_drops(conn, server="en", stage_code="CN-1", now=NOW_FRESH)
    assert cn_gs1.status == "not_found"
    assert en_cn1.status == "not_found"

    cn = get_stage_drops(conn, server="cn", stage_code="CN-1", now=NOW_FRESH)
    assert cn.status == "ok"
    assert cn.server == "cn" and cn.stage is not None and cn.stage.server == "cn"
    assert cn.stage.provenance.snapshot_id.startswith("cn:")
    # Every cn drop is region-tagged cn with a cn-region penguin snapshot.
    assert cn.drops and all(d.region == "cn" for d in cn.drops)
    assert all(d.snapshot_id.startswith("cn:") for d in cn.drops)


# --- attribution + active snapshots (§V53/§V27) -------------------------------


def test_accept_attribution_and_active_snapshots_surfaced(conn: sqlite3.Connection) -> None:
    registry = load_source_registry(REGISTRY)
    result = get_data_sources(registry, conn)
    penguin = next(s for s in result.sources if s.source_id == "penguin_statistics")
    view = penguin.entry.public_view()
    # §V53: penguin is CC BY-NC 4.0 -> license + attribution surfaced (§V27).
    assert view["license_identifier"] == "CC-BY-NC-4.0"
    assert "CC BY-NC 4.0" in str(view["attribution_text"])
    assert view["last_reviewed_at"]
    # §V27: never a local path / secret; policy_notes is withheld from the projection.
    assert "policy_notes" not in view
    # The imported drop cache is enriched as active en + cn penguin snapshots (§V54).
    servers = {s.server for s in penguin.active_snapshots}
    assert servers == {"en", "cn"}
