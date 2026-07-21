"""T97 (M9): the announcement-domain acceptance test (§V16, §V27, §V56, §V5).

The milestone gate for M9 (official-announcement metadata intelligence). It drives
an announcement feed through the entire M9 stack the way a CLI ``import`` would, then
reads it back through the shared-core services both transports call (§V14):

  build 4-4 candidate -> import_announcements (T95) for en + cn against a fixture
  fetcher (never a live fetch, §V1) -> reopen read-only -> get_announcements (T96) /
  get_data_sources (T27).

Each fixture feed entry carries a forbidden ``body``/``html``/``content``/``imageUrl``
alongside its metadata, so a correct metadata-only pipeline (importer field allowlist
-> repo -> service) is proven to drop every one of them -- from both the built DB and
the served result. The four milestone assertions:

* **disabled-by-default gate honored** (§V56/D14): the shipped registry ships both
  official-news sources ``enabled = False``; enabling is an explicit, reviewed act.
* **enabled source -> metadata rows, no body** (§V16/§V56): with the source imported,
  ``get_announcements`` returns exactly the five metadata fields + region; no body /
  html / prose / image survives into the DB or the wire.
* **en/cn separation** (§V5): an en query returns en-only rows, a cn query cn-only, in
  one DB; every fact carries its region + a region-prefixed provenance snapshot.
* **attribution + last_reviewed surfaced** (§V27): ``get_data_sources`` reports each
  announcement source's ``attribution_text`` + ``last_reviewed_at`` while withholding
  ``policy_notes`` (the field that may carry takedown correspondence) and any local
  path / secret.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.announcements import import_announcements
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.services.announcements import get_announcements
from arknights_mcp.services.source_status import get_data_sources
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: The two shipped official-news sources and their fact regions (§V5/§V56).
_ANNOUNCEMENT_SOURCES = {
    "arknights_global_official_news": "en",
    "arknights_cn_official_news": "cn",
}

#: The only keys a served announcement fact may carry: the five §V56 metadata fields
#: plus the explicit region (§V5). Anything else is a leak.
_ALLOWED_KEYS = {"announce_id", "title", "date", "url", "category", "region"}

#: Prose that rides every fixture feed entry on a NON-allowlisted key. A correct
#: metadata-only pipeline (§V16/§V56) drops it before storage; ASCII-only so JSON
#: escaping can never mask a leak in the DB dump or the served result.
_FORBIDDEN_PROSE = "ANNOUNCEBODYPROSE"

#: Two en announcements with distinct ISO dates so region + ordering are deterministic;
#: each carries a forbidden body/html/prose/image field that must never survive (§V16).
_EN_FEED: list[dict[str, Any]] = [
    {
        "announceId": "ann-en-1",
        "title": "Older Event",
        "date": "2026-07-01T00:00:00+00:00",
        "url": "https://www.arknights.global/news/ann-en-1",
        "category": "event",
        "body": _FORBIDDEN_PROSE + " the full article body that must never be stored",
        "html": "<p>" + _FORBIDDEN_PROSE + "</p>",
    },
    {
        "announceId": "ann-en-2",
        "title": "Newest Banner",
        "date": "2026-07-20T00:00:00+00:00",
        "url": "https://www.arknights.global/news/ann-en-2",
        "category": "banner",
        "content": _FORBIDDEN_PROSE + " more prose that must never be stored",
        "imageUrl": "https://cdn/" + _FORBIDDEN_PROSE + ".png",
    },
]

#: One cn announcement so a cn query returns cn-only data (en/cn never mixed, §V5).
_CN_FEED: list[dict[str, Any]] = [
    {
        "announceId": "ann-cn-1",
        "title": "CN 公告",
        "date": "2026-07-15T00:00:00+00:00",
        "url": "https://ak.hypergryph.com/news/ann-cn-1",
        "category": "maintenance",
        "body": _FORBIDDEN_PROSE + " cn body that must never be stored",
    },
]


class _FakeFetcher:
    """Returns a preset announcement feed payload (no network, §V1)."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def fetch(self) -> Any:
        return self._payload


def _candidate(tmp_path: Path) -> Path:
    """Build the 4-4 candidate, then import en + cn announcements via the real T95 path.

    ``build_candidate`` seeds the full source registry into ``data_sources`` (so the
    announcement snapshot FK holds), then the real importer runs against an in-memory
    fake fetcher on the writable candidate -- mirroring the CLI ``import`` shape before
    the read-only reopen (§V2).
    """
    path = tmp_path / "cand.sqlite"
    adapter = LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot")
    build_candidate(
        path,
        [ServerImport("en", adapter, "local_snapshot")],
        registry=load_source_registry(REGISTRY),
    )
    conn = sqlite3.connect(str(path))
    try:
        import_announcements(conn, _FakeFetcher(_EN_FEED), region="en")
        import_announcements(conn, _FakeFetcher(_CN_FEED), region="cn")
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """4-4 build with en + cn announcements imported, reopened read-only (§V2)."""
    return open_read_only(_candidate(tmp_path))


# --- disabled-by-default gate honored (§V56/D14) ------------------------------


def test_disabled_by_default_gate_honored() -> None:
    # §V56/D14: the shipped registry ships both official-news sources DISABLED; a real
    # sync would never fetch them until an explicit, reviewed enablement. The M9
    # importer landing does not flip the gate.
    reg = load_source_registry(REGISTRY, validate=False)
    for source_id, region in _ANNOUNCEMENT_SOURCES.items():
        entry = reg.get(source_id)
        assert entry is not None, f"missing announcement source: {source_id}"
        assert entry.enabled is False, f"{source_id} must be disabled by default (§V56/D14)"
        assert entry.regions == [region], f"{source_id} region must be [{region!r}] (§V5)"


# --- enabled source -> metadata rows, no body (§V16/§V56) ---------------------


def test_enabled_source_yields_metadata_rows(conn: sqlite3.Connection) -> None:
    # §V56: once imported, the source yields the announcement metadata rows, newest
    # first (§V26) -- exactly the five metadata fields + region, nothing else.
    result = get_announcements(conn, server="en")
    assert result.status == "ok"
    assert [a.announce_id for a in result.announcements] == ["ann-en-2", "ann-en-1"]
    for a in result.announcements:
        assert a.region == "en"
        assert a.title and a.url and a.category
        # The dataclass shape itself cannot hold a body/prose field -- its attributes
        # are exactly the metadata set (§V16 at the type level).
        assert set(vars(a)) == _ALLOWED_KEYS


def test_no_body_survives_to_result_or_db(conn: sqlite3.Connection) -> None:
    # §V16/§V56: the forbidden body/html/content/image prose rode every feed entry on a
    # non-allowlisted key -> stripped by the importer allowlist -> absent from BOTH the
    # served result and the whole built DB (schema cannot hold it; allowlist drops it).
    for server in ("en", "cn"):
        served = str([vars(a) for a in get_announcements(conn, server=server).announcements])
        assert _FORBIDDEN_PROSE not in served, f"{server}: prose leaked into the served result"

    tables = [
        name
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    db_dump = "\n".join(
        str(row)
        for table in tables
        for row in conn.execute(f"SELECT * FROM {table}")  # noqa: S608
    )
    assert _FORBIDDEN_PROSE not in db_dump, "prose leaked into the built DB"


# --- en/cn separation (§V5) ---------------------------------------------------


def test_en_and_cn_never_mixed(conn: sqlite3.Connection) -> None:
    # §V5: a cn query returns cn-only data; en announcements are never surfaced under it,
    # and vice versa -- in one DB. Every fact carries region + a region-prefixed
    # provenance snapshot (its own provenance chain, §V17).
    en = get_announcements(conn, server="en")
    cn = get_announcements(conn, server="cn")

    assert [a.announce_id for a in en.announcements] == ["ann-en-2", "ann-en-1"]
    assert all(a.region == "en" for a in en.announcements)
    assert en.provenance and all(p.snapshot_id.startswith("en:") for p in en.provenance)

    assert [a.announce_id for a in cn.announcements] == ["ann-cn-1"]
    assert all(a.region == "cn" for a in cn.announcements)
    assert cn.provenance and all(p.snapshot_id.startswith("cn:") for p in cn.provenance)

    # The two regions do not share a snapshot chain (en & cn never silently mixed).
    en_snaps = {p.snapshot_id for p in en.provenance}
    cn_snaps = {p.snapshot_id for p in cn.provenance}
    assert en_snaps.isdisjoint(cn_snaps)


# --- attribution + last_reviewed surfaced (§V27) ------------------------------


def test_get_data_sources_shows_attribution_and_last_reviewed(conn: sqlite3.Connection) -> None:
    # §V27: get_data_sources reports each announcement source's attribution + review
    # date through the single public projection, while withholding policy_notes (which
    # may carry takedown correspondence) and any local path / secret.
    result = get_data_sources(load_source_registry(REGISTRY), conn)
    by_id = {s.source_id: s for s in result.sources}
    for source_id in _ANNOUNCEMENT_SOURCES:
        info = by_id[source_id]
        view = info.to_dict()
        # §V27: attribution + last_reviewed present.
        assert str(view["attribution_text"]).strip(), f"{source_id}: attribution missing"
        assert view["last_reviewed_at"] == "2026-07-21", f"{source_id}: review date not surfaced"
        # §V27: the disabled posture is reported truthfully (the gate is public).
        assert view["enabled"] is False
        # §V27: policy_notes is withheld from every public projection.
        assert "policy_notes" not in view
        # §V16/§V56: no consumed field names a body/html/prose/image scope.
        for field in view["fields_consumed"]:  # type: ignore[union-attr]
            low = str(field).lower()
            assert not any(bad in low for bad in ("body", "html", "prose", "image"))
