"""§T96 ``get_announcements`` tool tests (§V5/§V19/§V22/§V23/§V56; §I.tool).

The tool is the model -> service -> envelope bridge for the announcement metadata
cache; these drive it end to end against the same production read-only path (§V2).
The announcements are seeded through the REAL T95 importer with an in-memory fake
fetcher (no live network, §V1), so the whole metadata-only pipeline (importer field
allowlist -> repo -> service -> tool) is exercised. They assert:

* the §V5 region + provenance ride every delivered result, and en announcements are
  never surfaced under a cn query (en/cn never mixed);
* the §V56/§V16 metadata-only contract: only announce_id/title/date/url/category/region
  reach the wire -- a seeded body/html never survives to the client;
* the optional since/until ISO date window narrows the list, newest-first;
* the §V19/§V22 bounded pagination: out-of-range page rejected at BOTH the model and
  the service (never a silent clamp), and the page descriptor reports total + has_more;
* a region with no announcements is a legitimate empty ``ok`` list (the adapter is
  disabled by default, D14/§V56), never a ``not_found``;
* the typed §V23 envelope shape, including fail-closed ``database_unavailable`` /
  ``internal_error`` with no path/trace leak;
* the §I.tool wire contract: a read-only spec with a bounded input schema, present in
  the single shared registry both transports dispatch (§V14).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from arknights_mcp.db.connection import DatabaseUnavailable, open_read_only
from arknights_mcp.importers.announcements import import_announcements
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.envelopes import SCHEMA_VERSION
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.mcp.tools.announcements import build_get_announcements_spec
from arknights_mcp.models.common import MAX_ID_LEN
from arknights_mcp.services.announcements import get_announcements
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: The five §V56 metadata keys the wire may carry, plus the explicit region (§V5).
_ALLOWED_KEYS = {"announce_id", "title", "date", "url", "category", "region"}

#: Three en announcements with distinct ISO dates so ordering + windowing are
#: deterministic; each carries forbidden body/html that must never survive (§V16).
_EN_FEED: list[dict[str, Any]] = [
    {
        "announceId": "ann-en-1",
        "title": "Older Event",
        "date": "2026-07-01T00:00:00+00:00",
        "url": "https://www.arknights.global/news/ann-en-1",
        "category": "event",
        "body": "the full article body that must never be stored",
        "html": "<p>prose</p>",
    },
    {
        "announceId": "ann-en-2",
        "title": "Middle Maintenance",
        "date": "2026-07-10T00:00:00+00:00",
        "url": "https://www.arknights.global/news/ann-en-2",
        "category": "maintenance",
        "content": "more prose that must never be stored",
    },
    {
        "announceId": "ann-en-3",
        "title": "Newest Banner",
        "date": "2026-07-20T00:00:00+00:00",
        "url": "https://www.arknights.global/news/ann-en-3",
        "category": "banner",
        "imageUrl": "https://cdn/x.png",
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
        "body": "cn body that must never be stored",
    },
]


class _FakeFetcher:
    """Returns a preset announcement feed payload (no network, §V1)."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def fetch(self) -> Any:
        return self._payload


def _candidate(tmp_path: Path, *, seed_en: bool = True, seed_cn: bool = False) -> Path:
    """Build the 4-4 fixture candidate, then import announcements via the real T95 path.

    Opens a read-write handle onto the freshly built candidate (before promotion +
    read-only reopen, mirroring the importer's own shape) and runs
    ``import_announcements`` with a fake fetcher; ``build_candidate`` already seeded the
    announcement sources into ``data_sources`` (the full registry), so the snapshot FK
    holds.
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
        if seed_en:
            import_announcements(conn, _FakeFetcher(_EN_FEED), region="en")
        if seed_cn:
            import_announcements(conn, _FakeFetcher(_CN_FEED), region="cn")
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """4-4 build with en + cn announcements imported."""
    return open_read_only(_candidate(tmp_path, seed_en=True, seed_cn=True))


@pytest.fixture
def bare_conn(tmp_path: Path) -> sqlite3.Connection:
    """4-4 build with NO announcements imported (the empty-domain case)."""
    return open_read_only(_candidate(tmp_path, seed_en=False, seed_cn=False))


def _handler(conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    return build_get_announcements_spec(lambda: conn).handler


# --- metadata facts + §V5 region + provenance ---------------------------------


def test_ok_returns_announcement_metadata(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en")
    assert env.status == "ok"
    assert env.schema_version == SCHEMA_VERSION
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert set(data) == {"announcements", "page"}
    anns = data["announcements"]
    assert isinstance(anns, list) and len(anns) == 3
    # §V26: newest first.
    assert [a["announce_id"] for a in anns] == ["ann-en-3", "ann-en-2", "ann-en-1"]
    for a in anns:
        assert a["region"] == "en"


def test_ok_carries_region_and_provenance(conn: sqlite3.Connection) -> None:
    # §V5: every delivered fact carries region + provenance.
    prov = _handler(conn)(server="en").to_dict()["provenance"]
    assert isinstance(prov, list) and len(prov) == 1
    assert prov[0]["server"] == "en"
    assert prov[0]["snapshot_id"] and prov[0]["imported_at"]


def test_en_and_cn_never_mixed(conn: sqlite3.Connection) -> None:
    # §V5/§V56: a cn query returns cn-only data; en announcements are not surfaced.
    env = _handler(conn)(server="cn")
    assert env.status == "ok"
    anns = env.to_dict()["data"]["announcements"]  # type: ignore[index]
    assert [a["announce_id"] for a in anns] == ["ann-cn-1"]
    assert all(a["region"] == "cn" for a in anns)


# --- metadata-only: no body/html/prose survives (§V56/§V16) -------------------


def test_no_prose_fields_surface(conn: sqlite3.Connection) -> None:
    anns = _handler(conn)(server="en").to_dict()["data"]["announcements"]  # type: ignore[index]
    for a in anns:
        assert set(a) <= _ALLOWED_KEYS
        for forbidden in ("body", "html", "content", "imageUrl", "image_url"):
            assert forbidden not in a


# --- since/until date window --------------------------------------------------


def test_since_filters_older(conn: sqlite3.Connection) -> None:
    anns = _handler(conn)(server="en", since="2026-07-05T00:00:00+00:00").to_dict()["data"][
        "announcements"
    ]  # type: ignore[index]
    assert [a["announce_id"] for a in anns] == ["ann-en-3", "ann-en-2"]


def test_until_filters_newer(conn: sqlite3.Connection) -> None:
    anns = _handler(conn)(server="en", until="2026-07-15T00:00:00+00:00").to_dict()["data"][
        "announcements"
    ]  # type: ignore[index]
    assert [a["announce_id"] for a in anns] == ["ann-en-2", "ann-en-1"]


def test_since_and_until_window(conn: sqlite3.Connection) -> None:
    anns = _handler(conn)(
        server="en", since="2026-07-05T00:00:00+00:00", until="2026-07-15T00:00:00+00:00"
    ).to_dict()["data"]["announcements"]  # type: ignore[index]
    assert [a["announce_id"] for a in anns] == ["ann-en-2"]


# --- §V19/§V22 bounded pagination ---------------------------------------------


def test_pagination_slices_and_reports_total(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", page={"page": 1, "page_size": 2})
    data = env.to_dict()["data"]
    anns = data["announcements"]  # type: ignore[index]
    page = data["page"]  # type: ignore[index]
    assert [a["announce_id"] for a in anns] == ["ann-en-3", "ann-en-2"]
    assert page == {"page": 1, "page_size": 2, "total": 3, "has_more": True}

    env2 = _handler(conn)(server="en", page={"page": 2, "page_size": 2})
    data2 = env2.to_dict()["data"]
    assert [a["announce_id"] for a in data2["announcements"]] == ["ann-en-1"]  # type: ignore[index]
    assert data2["page"]["has_more"] is False  # type: ignore[index]


def test_out_of_range_page_rejected_at_model(conn: sqlite3.Connection) -> None:
    # §V19: rejected at the model gate, never silently widened into a dump.
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", page={"page": 1, "page_size": 101})
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", page={"page": 0, "page_size": 10})


def test_out_of_range_page_rejected_at_service(conn: sqlite3.Connection) -> None:
    # §V19: a caller reaching the service directly (bypassing the model) gets the SAME
    # rejection, not a silent clamp -- one contract, both places.
    with pytest.raises(ValueError, match="§V19"):
        get_announcements(conn, server="en", page_size=101)
    with pytest.raises(ValueError, match="§V19"):
        get_announcements(conn, server="en", page=0)


# --- empty domain: ok empty list, never not_found (§V56/§V23) -----------------


def test_empty_region_is_ok_empty_list(bare_conn: sqlite3.Connection) -> None:
    # §V56: the announcement source is disabled by default, so a region with no
    # imported feed is a legitimate empty ``ok`` list, not a ``not_found``.
    env = _handler(bare_conn)(server="en")
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert data["announcements"] == []  # type: ignore[index]
    assert data["page"] == {"page": 1, "page_size": 50, "total": 0, "has_more": False}  # type: ignore[index]
    assert env.to_dict()["provenance"] == []


# --- §V23 fail-closed ---------------------------------------------------------


def test_database_unavailable_fails_closed() -> None:
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: /home/ubuntu/cand.sqlite")

    env = build_get_announcements_spec(boom).handler(server="en")
    assert env.status == "database_unavailable"
    body = str(env.to_dict()["data"])
    assert "/home/ubuntu" not in body
    assert "Traceback" not in body


def test_internal_error_fails_closed() -> None:
    def boom() -> sqlite3.Connection:
        raise RuntimeError("unexpected: /secret/path")

    env = build_get_announcements_spec(boom).handler(server="en")
    assert env.status == "internal_error"
    body = str(env.to_dict()["data"])
    assert "/secret/path" not in body
    assert "Traceback" not in body


# --- invalid input rejected at the model gate ---------------------------------


def test_missing_server_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(conn)()


def test_bad_region_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(conn)(server="jp")


def test_unknown_parameter_rejected(conn: sqlite3.Connection) -> None:
    # §V18: extra="forbid" -- a crafted request cannot smuggle a field.
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", bogus=1)


def test_oversized_since_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", since="x" * (MAX_ID_LEN + 1))


# --- §V14 shared registry + §I.tool wire contract -----------------------------


def test_registered_in_shared_registry(conn: sqlite3.Connection) -> None:
    registry = build_tool_registry(
        lambda: conn, registry=load_source_registry(REGISTRY), mode="local"
    )
    assert "get_announcements" in registry.names()
    spec = registry.get("get_announcements")
    assert spec.read_only is True
    schema = spec.input_schema
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
