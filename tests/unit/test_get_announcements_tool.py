"""``get_announcements`` tool tests.

The tool is the model -> service -> envelope bridge for the announcement metadata
cache; these drive it end to end against the same production read-only path.
The announcements are seeded through the REAL importer with an in-memory fake
fetcher (no live network), so the whole metadata-only pipeline (importer field
allowlist -> repo -> service -> tool) is exercised. They assert:

* the region + provenance ride every delivered result, and en announcements are
  never surfaced under a cn query (en/cn never mixed);
* the metadata-only contract: only announce_id/title/date/url/category/region
  reach the wire -- a seeded body/html never survives to the client;
* the optional since/until ISO date window narrows the list, newest-first;
* the bounded pagination: out-of-range page rejected at BOTH the model and
  the service (never a silent clamp), and the page descriptor reports total + has_more;
* a region with no announcements is a legitimate empty ``ok`` list, never a
  ``not_found`` -- and never a BARE one: availability is decided before absence is
  asserted, so a never-imported feed and a live feed with nothing in the window carry
  DIFFERENT limitations;
* the typed envelope shape, including fail-closed ``database_unavailable`` /
  ``internal_error`` with no path/trace leak;
* the wire contract: a read-only spec with a bounded input schema, present in
  the single shared registry both transports dispatch.
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

#: The five metadata keys a wire row may carry: no per-row
#: ``region`` -- it rides the parent ``server`` field once, never on every row.
_ALLOWED_KEYS = {"announce_id", "title", "date", "url", "category"}

#: Three en announcements with distinct ISO dates so ordering + windowing are
#: deterministic; each carries forbidden body/html that must never survive.
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

#: One cn announcement so a cn query returns cn-only data (en/cn never mixed).
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
    """Returns a preset announcement feed payload (no network)."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def fetch(self) -> Any:
        return self._payload


def _candidate(
    tmp_path: Path, *, seed_en: bool = True, seed_cn: bool = False, name: str = "cand.sqlite"
) -> Path:
    """Build the 4-4 fixture candidate, then import announcements via the real path.

    Opens a read-write handle onto the freshly built candidate (before promotion +
    read-only reopen, mirroring the importer's own shape) and runs
    ``import_announcements`` with a fake fetcher; ``build_candidate`` already seeded the
    announcement sources into ``data_sources`` (the full registry), so the snapshot FK
    holds. ``name`` keeps two differently-seeded builds apart in one ``tmp_path``, so a
    test may compare an imported feed against a never-imported one.
    """
    path = tmp_path / name
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
    return open_read_only(_candidate(tmp_path, seed_en=False, seed_cn=False, name="bare.sqlite"))


@pytest.fixture
def en_only_conn(tmp_path: Path) -> sqlite3.Connection:
    """4-4 build with the en feed imported and the cn feed never run (per region)."""
    return open_read_only(_candidate(tmp_path, seed_en=True, seed_cn=False, name="en_only.sqlite"))


def _handler(conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    return build_get_announcements_spec(lambda: conn).handler


# --- metadata facts + region + provenance -------------------------------------


def test_ok_returns_announcement_metadata(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en")
    assert env.status == "ok"
    assert env.schema_version == SCHEMA_VERSION
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert set(data) == {"server", "announcements", "page"}
    # Region stated ONCE on the parent server, never per row.
    assert data["server"] == "en"
    anns = data["announcements"]
    assert isinstance(anns, list) and len(anns) == 3
    # Newest first.
    assert [a["announce_id"] for a in anns] == ["ann-en-3", "ann-en-2", "ann-en-1"]
    for a in anns:
        assert "region" not in a


def test_ok_carries_region_and_provenance(conn: sqlite3.Connection) -> None:
    # Every delivered fact carries region + provenance.
    prov = _handler(conn)(server="en").to_dict()["provenance"]
    assert isinstance(prov, list) and len(prov) == 1
    assert prov[0]["server"] == "en"
    assert prov[0]["snapshot_id"] and prov[0]["imported_at"]


def test_en_and_cn_never_mixed(conn: sqlite3.Connection) -> None:
    # A cn query returns cn-only data; en announcements are not surfaced.
    env = _handler(conn)(server="cn")
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert data["server"] == "cn"  # type: ignore[index]
    anns = data["announcements"]  # type: ignore[index]
    assert [a["announce_id"] for a in anns] == ["ann-cn-1"]
    assert all("region" not in a for a in anns)


# --- metadata-only: no body/html/prose survives -------------------------------


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


# --- bounded pagination -------------------------------------------------------


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
    # Rejected at the model gate, never silently widened into a dump.
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", page={"page": 1, "page_size": 101})
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", page={"page": 0, "page_size": 10})


def test_out_of_range_page_rejected_at_service(conn: sqlite3.Connection) -> None:
    # A caller reaching the service directly (bypassing the model) gets the SAME
    # rejection, not a silent clamp -- one contract, both places.
    with pytest.raises(ValueError, match="outside the"):
        get_announcements(conn, server="en", page_size=101)
    with pytest.raises(ValueError, match="must be >= 1"):
        get_announcements(conn, server="en", page=0)


# --- empty domain: ok empty list, never not_found -----------------------------


def test_empty_region_is_ok_empty_list(bare_conn: sqlite3.Connection) -> None:
    # An empty answer to a well-formed set query is ``ok`` + an empty
    # collection, never a ``not_found`` (that would be an entity-lookup verdict).
    env = _handler(bare_conn)(server="en")
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert data["announcements"] == []  # type: ignore[index]
    assert data["page"] == {"page": 1, "page_size": 50, "total": 0, "has_more": False}  # type: ignore[index]
    assert env.to_dict()["provenance"] == []


# --- availability BEFORE absence ----------------------------------------------


def test_unimported_feed_names_the_source_and_the_admin_action(
    bare_conn: sqlite3.Connection,
) -> None:
    # With no announcement snapshot for the region, "no announcement" is not
    # inferable -- the empty list must say the feed never ran, name the source, and
    # give the admin step (importing is CLI-only, never a query-time fetch).
    env = _handler(bare_conn)(server="en")
    assert env.status == "ok"
    limitations = env.to_dict()["limitations"]
    assert isinstance(limitations, list) and len(limitations) == 1
    text = limitations[0]
    assert "arknights_global_official_news" in text
    assert "no imported snapshot" in text
    assert "arknights-mcp sync --server en" in text


def test_availability_verdict_is_per_region(en_only_conn: sqlite3.Connection) -> None:
    # The probe is the REGION's own feed. en imported + cn not is a normal
    # build state, and a sibling region's success must not vouch for cn.
    assert _handler(en_only_conn)(server="en").limitations == ()
    cn = _handler(en_only_conn)(server="cn").to_dict()["limitations"]
    assert isinstance(cn, list) and len(cn) == 1
    assert "arknights_cn_official_news" in cn[0]
    assert "arknights-mcp sync --server cn" in cn[0]


def test_empty_window_on_an_imported_feed_says_so_differently(
    conn: sqlite3.Connection, bare_conn: sqlite3.Connection
) -> None:
    # The original defect: an unimported feed and a live feed with nothing in the window
    # shipped identical bytes, so the response could not be read either way. Both are
    # still ``ok`` + ``[]``, but the limitation now decides it -- and the imported one
    # never suggests a sync, which would read as "the cache is missing".
    # A DAY bound: the feed's own column is day-granular, so a sub-day bound would also
    # carry the widening disclosure, which is a different fact tested in
    # ``test_window_bound_form.py`` -- this case is about the two EMPTY reasons.
    windowed = _handler(conn)(server="en", since="2027-01-01")
    unimported = _handler(bare_conn)(server="en")
    assert windowed.status == unimported.status == "ok"
    assert windowed.to_dict()["data"]["announcements"] == []  # type: ignore[index]
    assert unimported.to_dict()["data"]["announcements"] == []  # type: ignore[index]

    assert windowed.limitations != unimported.limitations
    (empty,) = windowed.limitations
    assert "is imported for this region" in empty
    assert "since/until" in empty
    assert "arknights-mcp sync" not in empty


def test_non_empty_result_carries_no_availability_limitation(conn: sqlite3.Connection) -> None:
    # The rows themselves prove the feed is present; a caveat here would be noise.
    assert _handler(conn)(server="en").limitations == ()


def test_service_direct_unsupported_region_states_it_has_no_feed(
    conn: sqlite3.Connection,
) -> None:
    # Unreachable through the tool (the model admits only en/cn) but reachable through
    # the service: an empty list for a region that HAS no feed must not read as "this
    # region published nothing".
    result = get_announcements(conn, server="jp")
    assert result.status == "ok" and result.announcements == ()
    (text,) = result.limitations
    assert "no official announcement feed for region jp" in text


# --- fail-closed --------------------------------------------------------------


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
    # extra="forbid" -- a crafted request cannot smuggle a field.
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", bogus=1)


def test_oversized_since_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", since="x" * (MAX_ID_LEN + 1))


# --- shared registry + wire contract ------------------------------------------


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
