"""``get_banners`` tool tests.

The tool is the model -> service -> envelope bridge for the banner archive; these drive
it end to end against the same production read-only path. Banners are seeded
through the REAL ``insert_banners`` writer (soft-resolve + provenance + DB insert),
so the whole metadata-only read path (writer -> repo -> service -> tool) is exercised.
They assert:

* the region + provenance ride every delivered result, and en banners are never
  surfaced under a cn query (en/cn never mixed);
* the metadata-only contract: only the schedule/identity fields + the TYPED
  featured ops reach the wire -- no gacha summary/detail/html/image;
* the caveats: a LIMITED banner resolves its featured op to an operator name; a
  standard (NORMAL) banner carries no featured op and surfaces the standard-banner
  limitation; an unresolved featured op surfaces its raw char id + the unresolved caveat;
* A pool with no typed featured op OMITS ``featured_ops`` instead of
  sending ``[]``, and the reason splits by rule type -- an expected CARRIER with no array
  (FESCLASSIC), or a rule type never classified (BACKFLOW), names itself in its own
  caveat rather than borrowing the standard banner's prose-only explanation;
* the optional since/until ISO open-time window narrows the list, newest-first;
* the bounded pagination: out-of-range page rejected at BOTH the model and the
  service (never a silent clamp), and the page descriptor reports total + has_more;
* a region with no banners is a legitimate empty ``ok`` list (gacha_table is tolerant-
  absent), never a ``not_found``;
* the typed envelope shape, including fail-closed ``database_unavailable`` /
  ``internal_error`` with no path/trace leak;
* the wire contract: a read-only spec with a bounded input schema, present in the
  single shared registry both transports dispatch.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from arknights_mcp.db.connection import DatabaseUnavailable, open_read_only
from arknights_mcp.db.migrations import build_database
from arknights_mcp.importers.banners import ParsedBanner, insert_banners
from arknights_mcp.mcp.envelopes import SCHEMA_VERSION
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.mcp.tools.banners import build_get_banners_spec
from arknights_mcp.models.common import MAX_ID_LEN, MAX_QUERY_LEN
from arknights_mcp.services.banners import (
    STANDARD_BANNER_LIMITATION,
    UNRESOLVED_FEATURED_OP_LIMITATION,
    absent_featured_op_array_limitation,
    get_banners,
)
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"
_SOURCE_ID = "local_snapshot"

#: The metadata keys a banner row may carry on the wire; never gacha prose.
#: No per-row ``region`` -- it rides the parent ``server`` field once.
_ALLOWED_KEYS = {
    "game_id",
    "display_name",
    "open_time",
    "end_time",
    "rule_type",
    "featured_ops",
}

#: Three en banners with distinct open_times so ordering + windowing are deterministic:
#: a LIMITED banner (featured op resolves to the seeded operator), a CLASSIC banner
#: (one resolved + one unresolved featured op), and a NORMAL standard banner (no
#: featured op -> standard-banner limitation).
_EN_BANNERS = [
    ParsedBanner(
        game_id="LIMITED_1",
        display_name="Limited Headhunting",
        open_time="2026-07-20T00:00:00+00:00",
        end_time="2026-07-27T00:00:00+00:00",
        rule_type="LIMITED",
        featured_char_ids=["char_002_amiya"],
        provenance_record={"gachaPoolId": "LIMITED_1"},
    ),
    ParsedBanner(
        game_id="CLASSIC_1",
        display_name="Classic Headhunting",
        open_time="2026-07-10T00:00:00+00:00",
        end_time="2026-07-17T00:00:00+00:00",
        rule_type="CLASSIC",
        featured_char_ids=["char_002_amiya", "char_999_ghost"],
        provenance_record={"gachaPoolId": "CLASSIC_1"},
    ),
    ParsedBanner(
        game_id="NORMAL_1",
        display_name="Standard Headhunting",
        open_time="2026-07-01T00:00:00+00:00",
        end_time="2026-07-08T00:00:00+00:00",
        rule_type="NORMAL",
        featured_char_ids=[],
        provenance_record={"gachaPoolId": "NORMAL_1"},
    ),
]

#: Two cn banners so a cn query returns cn-only data (en/cn never mixed): a LIMITED
#: pool with a featured op, and the FESCLASSIC case -- a rule type that
#: declares a typed CARRIER, whose featured-op array the source does not actually have.
#: That pool is why the cn listing is the one that proves the two absence caveats are
#: DISTINCT: it must take the absent-array arm without ever taking the standard-banner arm.
_CN_BANNERS = [
    ParsedBanner(
        game_id="CN_1",
        display_name="CN 限定",
        open_time="2026-07-15T00:00:00+00:00",
        end_time="2026-07-22T00:00:00+00:00",
        rule_type="LIMITED",
        featured_char_ids=["char_002_amiya"],
        provenance_record={"gachaPoolId": "CN_1"},
    ),
    ParsedBanner(
        game_id="CN_FES_1",
        display_name="CN Celebration",
        open_time="2026-07-02T00:00:00+00:00",
        end_time="2026-07-09T00:00:00+00:00",
        rule_type="FESCLASSIC",
        featured_char_ids=[],
        provenance_record={"gachaPoolId": "CN_FES_1"},
    ),
]


def _seed_registry(conn: sqlite3.Connection) -> None:
    """Seed the primary source + one snapshot per region (en/cn) + an en operator."""
    conn.execute(
        "INSERT INTO data_sources (source_id, display_name, owner_name, canonical_url, "
        "source_type, regions_json, adapter_version, license_status, permission_status, "
        "redistribution_status, attribution_text, enabled, last_reviewed_at) VALUES "
        "(?, 'gd', 'o', 'https://x/', 'game_data_repository', '[\"en\",\"cn\"]', '0', "
        "'reviewed', 'reviewed', 'derived', 'a', 1, '2026-07-21')",
        (_SOURCE_ID,),
    )
    for snap, server in (("snap-en", "en"), ("snap-cn", "cn")):
        conn.execute(
            "INSERT INTO source_snapshots (snapshot_id, source_id, server, imported_at, "
            "manifest_hash, status, field_policy_version) VALUES "
            "(?, ?, ?, '2026-07-21T00:00:00+00:00', 'h', 'active', 'test')",
            (snap, _SOURCE_ID, server),
        )
    # An en operator so the LIMITED/CLASSIC featured char_002_amiya soft-resolves.
    prov = conn.execute(
        "INSERT INTO record_provenance (snapshot_id, source_path, source_record_key, "
        "record_hash, transform_version, field_policy_version) VALUES "
        "('snap-en', 'gamedata/excel/character_table.json', 'char_002_amiya', 'rh', "
        "'test', 'test')"
    ).lastrowid
    conn.execute(
        "INSERT INTO operators (server, game_id, display_name, provenance_id) "
        "VALUES ('en', 'char_002_amiya', 'Amiya', ?)",
        (prov,),
    )


def _candidate(
    tmp_path: Path,
    *,
    seed_en: bool = True,
    seed_cn: bool = False,
    extra_en: Sequence[ParsedBanner] = (),
    extra_cn: Sequence[ParsedBanner] = (),
) -> Path:
    """Build a bare candidate DB, seed the registry, and import the banner archive.

    Uses the real ``insert_banners`` writer so soft-resolve + provenance + region
    stamping are exercised, then closes so the tool reopens the file read-only.
    ``extra_en``/``extra_cn`` seed a pool ON TOP of the shared listing for a test that
    needs one rule type the default fixtures do not carry -- they must be passed here,
    because the connection every test reads through is read-only and cannot be
    written mid-test.
    """
    path = tmp_path / "cand.sqlite"
    conn = build_database(path)
    try:
        _seed_registry(conn)
        if seed_en:
            insert_banners(
                conn,
                [*_EN_BANNERS, *extra_en],
                server="en",
                snapshot_id="snap-en",
                source_path="gamedata/excel/gacha_table.json",
            )
        if seed_cn:
            insert_banners(
                conn,
                [*_CN_BANNERS, *extra_cn],
                server="cn",
                snapshot_id="snap-cn",
                source_path="gamedata/excel/gacha_table.json",
            )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Candidate with en + cn banners imported."""
    return open_read_only(_candidate(tmp_path, seed_en=True, seed_cn=True))


@pytest.fixture
def bare_conn(tmp_path: Path) -> sqlite3.Connection:
    """Candidate with NO banners imported (the empty-domain case)."""
    return open_read_only(_candidate(tmp_path, seed_en=False, seed_cn=False))


def _handler(conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    return build_get_banners_spec(lambda: conn).handler


# --- metadata facts + region + provenance -------------------------------------


def test_ok_returns_banner_metadata(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en")
    assert env.status == "ok"
    assert env.schema_version == SCHEMA_VERSION
    data = env.to_dict()["data"]
    assert isinstance(data, dict)
    assert set(data) == {"server", "banners", "page", "enum_legend"}
    # Region stated ONCE on the parent server, never per row.
    assert data["server"] == "en"
    banners = data["banners"]
    assert isinstance(banners, list) and len(banners) == 3
    # Newest first (open_time DESC).
    assert [b["game_id"] for b in banners] == ["LIMITED_1", "CLASSIC_1", "NORMAL_1"]
    for b in banners:
        assert "region" not in b


def test_ok_carries_region_and_provenance(conn: sqlite3.Connection) -> None:
    prov = _handler(conn)(server="en").to_dict()["provenance"]
    assert isinstance(prov, list) and len(prov) == 1
    assert prov[0]["server"] == "en"
    assert prov[0]["snapshot_id"] and prov[0]["imported_at"]


def test_en_and_cn_never_mixed(conn: sqlite3.Connection) -> None:
    # A cn query returns cn-only data; en banners are not surfaced.
    env = _handler(conn)(server="cn")
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert data["server"] == "cn"  # type: ignore[index]
    banners = data["banners"]  # type: ignore[index]
    assert [b["game_id"] for b in banners] == ["CN_1", "CN_FES_1"]
    assert all("region" not in b for b in banners)


# --- typed featured ops + limitations -----------------------------------------


def _by_id(env, game_id: str) -> dict:  # type: ignore[no-untyped-def]
    (banner,) = [b for b in env.to_dict()["data"]["banners"] if b["game_id"] == game_id]
    return banner


def test_limited_featured_op_resolves_to_operator(conn: sqlite3.Connection) -> None:
    # A LIMITED banner's featured char id resolves to the present operator's name.
    banner = _by_id(_handler(conn)(server="en"), "LIMITED_1")
    assert banner["featured_ops"] == [
        {"char_id": "char_002_amiya", "resolved": True, "operator_name": "Amiya"}
    ]


def test_classic_family_partial_resolve(conn: sqlite3.Connection) -> None:
    # A CLASSIC banner names an array; the absent operator stays a raw char id.
    ops = _by_id(_handler(conn)(server="en"), "CLASSIC_1")["featured_ops"]
    assert {o["char_id"] for o in ops} == {"char_002_amiya", "char_999_ghost"}
    resolved = {o["char_id"]: o for o in ops}
    assert resolved["char_002_amiya"] == {
        "char_id": "char_002_amiya",
        "resolved": True,
        "operator_name": "Amiya",
    }
    # The unresolved op keeps its raw id and drops the name key
    # rather than carrying ``operator_name: null``. ``resolved: False`` is the typed
    # signal that the id had no name in this build -- a null beside it said the same
    # thing a second, weaker way (never null AND a flag for one absence).
    assert resolved["char_999_ghost"] == {"char_id": "char_999_ghost", "resolved": False}


def test_standard_banner_omits_featured_ops_and_notes_the_caveat(conn: sqlite3.Connection) -> None:
    # NORMAL carries no typed featured-op, so the key is
    # ABSENT rather than ``[]``. An empty list is the CONFIRMED-none, and the pool has
    # a rate-up -- it just lives in prose the field policy excludes. The caveat is the
    # sole signal (never a placeholder value AND a limitation for one absence).
    env = _handler(conn)(server="en")
    assert "featured_ops" not in _by_id(env, "NORMAL_1")
    assert STANDARD_BANNER_LIMITATION in env.limitations


def test_expected_carrier_without_the_array_names_its_rule_type(conn: sqlite3.Connection) -> None:
    # The client harm: FESCLASSIC is one of the six rule types declared a
    # typed carrier, and on the real corpus it carries the array on ZERO pools. The old wire
    # said ``featured_ops: []`` -- a CONFIRMED-none asserting a celebration banner has no
    # rate-up operator. Now the key is absent and the caveat NAMES the rule type, so the
    # reason resolves per pool through that row's own rule_type.
    env = _handler(conn)(server="cn")
    assert "featured_ops" not in _by_id(env, "CN_FES_1")
    caveat = absent_featured_op_array_limitation(("FESCLASSIC",))
    assert caveat in env.limitations
    # ... and it is NOT described as a standard banner whose rate-up lives in prose: that
    # mislabel is exactly the original defect, since the description's standard set
    # (NORMAL/SINGLE/DOUBLE/LINKAGE) never covered FESCLASSIC.
    assert STANDARD_BANNER_LIMITATION not in env.limitations


def test_both_absence_caveats_ride_a_mixed_page(tmp_path: Path) -> None:
    # A page mixing a standard pool with an expected-carrier pool carries BOTH
    # caveats, because the two absences have different causes and one caveat cannot stand
    # in for the other.
    path = _candidate(
        tmp_path,
        extra_en=[
            ParsedBanner(
                game_id="EN_FES_1",
                display_name="EN Celebration",
                open_time="2026-06-01T00:00:00+00:00",
                end_time="2026-06-08T00:00:00+00:00",
                rule_type="FESCLASSIC",
                featured_char_ids=[],
                provenance_record={"gachaPoolId": "EN_FES_1"},
            )
        ],
    )
    env = _handler(open_read_only(path))(server="en")
    assert STANDARD_BANNER_LIMITATION in env.limitations
    assert absent_featured_op_array_limitation(("FESCLASSIC",)) in env.limitations


def test_unclassified_rule_type_takes_the_conservative_arm(tmp_path: Path) -> None:
    # Unknown token -> conservative side + limitation: BACKFLOW is a real cn rule
    # type the list never named, and a token upstream adds tomorrow behaves the same.
    # It must surface as a NAMED source gap, never be read as a prose-only standard banner
    # -- a silent bucket is how a new pool type would inherit a claim nobody checked.
    path = _candidate(
        tmp_path,
        seed_en=False,
        seed_cn=True,
        extra_cn=[
            ParsedBanner(
                game_id="CN_BACKFLOW_1",
                display_name="CN Backflow",
                open_time="2026-06-01T00:00:00+00:00",
                end_time="2026-06-08T00:00:00+00:00",
                rule_type="BACKFLOW",
                featured_char_ids=[],
                provenance_record={"gachaPoolId": "CN_BACKFLOW_1"},
            )
        ],
    )
    env = _handler(open_read_only(path))(server="cn")
    assert "featured_ops" not in _by_id(env, "CN_BACKFLOW_1")
    assert absent_featured_op_array_limitation(("BACKFLOW", "FESCLASSIC")) in env.limitations
    assert STANDARD_BANNER_LIMITATION not in env.limitations


def test_unresolved_featured_op_limitation(conn: sqlite3.Connection) -> None:
    # An unresolved featured op is surfaced + noted as a caveat.
    env = _handler(conn)(server="en")
    assert UNRESOLVED_FEATURED_OP_LIMITATION in env.limitations


def test_no_standard_limitation_when_no_standard_pool_is_listed(conn: sqlite3.Connection) -> None:
    # The cn listing carries no NORMAL/SINGLE/DOUBLE/LINKAGE pool at all, so the
    # standard-banner caveat is absent -- the limitation is data-driven, not always-on, and
    # the FESCLASSIC pool it does carry takes the other arm instead.
    env = _handler(conn)(server="cn")
    assert STANDARD_BANNER_LIMITATION not in env.limitations


def test_no_absence_caveat_when_every_listed_pool_is_featured(conn: sqlite3.Connection) -> None:
    # Both absence caveats stay off a page where every pool carries a typed
    # featured op -- the cn LIMITED pool alone, reached through the query filter.
    env = _handler(conn)(server="cn", query="限定")
    assert STANDARD_BANNER_LIMITATION not in env.limitations
    assert absent_featured_op_array_limitation(("FESCLASSIC",)) not in env.limitations


# --- metadata-only: no gacha prose surfaces ----------------------------------


def test_no_prose_fields_surface(conn: sqlite3.Connection) -> None:
    banners = _handler(conn)(server="en").to_dict()["data"]["banners"]  # type: ignore[index]
    for b in banners:
        assert set(b) <= _ALLOWED_KEYS
        for forbidden in ("gachaPoolSummary", "gachaPoolDetail", "dynMeta", "html", "image"):
            assert forbidden not in b


# --- since/until open-time window ---------------------------------------------


def test_since_filters_older(conn: sqlite3.Connection) -> None:
    banners = _handler(conn)(server="en", since="2026-07-05T00:00:00+00:00").to_dict()["data"][
        "banners"
    ]  # type: ignore[index]
    assert [b["game_id"] for b in banners] == ["LIMITED_1", "CLASSIC_1"]


def test_until_filters_newer(conn: sqlite3.Connection) -> None:
    banners = _handler(conn)(server="en", until="2026-07-15T00:00:00+00:00").to_dict()["data"][
        "banners"
    ]  # type: ignore[index]
    assert [b["game_id"] for b in banners] == ["CLASSIC_1", "NORMAL_1"]


def test_since_and_until_window(conn: sqlite3.Connection) -> None:
    banners = _handler(conn)(
        server="en", since="2026-07-05T00:00:00+00:00", until="2026-07-15T00:00:00+00:00"
    ).to_dict()["data"]["banners"]  # type: ignore[index]
    assert [b["game_id"] for b in banners] == ["CLASSIC_1"]


def test_date_only_until_is_inclusive_of_that_day(conn: sqlite3.Connection) -> None:
    # A bare-date until (the model's advertised YYYY-MM-DD shape) must INCLUDE a banner
    # opening on that date even though open_time is a full timestamp -- a plain
    # lexicographic "open_time <= until" would drop LIMITED_1 (opens 2026-07-20T00:00:00)
    # because the timestamp sorts after its own date prefix. Inclusive-until means the
    # whole day is kept.
    banners = _handler(conn)(server="en", until="2026-07-20").to_dict()["data"]["banners"]  # type: ignore[index]
    assert [b["game_id"] for b in banners] == ["LIMITED_1", "CLASSIC_1", "NORMAL_1"]

    # A bare-date until strictly before the newest banner still excludes it.
    older = _handler(conn)(server="en", until="2026-07-19").to_dict()["data"]["banners"]  # type: ignore[index]
    assert [b["game_id"] for b in older] == ["CLASSIC_1", "NORMAL_1"]


# --- optional query display-name filter (additive) ---------------------------


def test_query_narrows_by_display_name_substring(conn: sqlite3.Connection) -> None:
    # A query narrows the list to banners whose display_name contains it; only
    # "Limited Headhunting" carries "Limited".
    banners = _handler(conn)(server="en", query="Limited").to_dict()["data"]["banners"]  # type: ignore[index]
    assert [b["game_id"] for b in banners] == ["LIMITED_1"]


def test_query_is_case_insensitive(conn: sqlite3.Connection) -> None:
    # SQLite's default LIKE is case-insensitive for ASCII, so a lowercased query matches.
    banners = _handler(conn)(server="en", query="limited").to_dict()["data"]["banners"]  # type: ignore[index]
    assert [b["game_id"] for b in banners] == ["LIMITED_1"]


def test_query_matches_shared_substring_newest_first(conn: sqlite3.Connection) -> None:
    # All three en banners share "Headhunting"; the filtered set is still newest-first.
    banners = _handler(conn)(server="en", query="Headhunting").to_dict()["data"]["banners"]  # type: ignore[index]
    assert [b["game_id"] for b in banners] == ["LIMITED_1", "CLASSIC_1", "NORMAL_1"]


def test_query_no_match_is_ok_empty_list(conn: sqlite3.Connection) -> None:
    # A query matching nothing is a legitimate empty ``ok`` list, never a ``not_found``.
    env = _handler(conn)(server="en", query="Nonexistent")
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert data["banners"] == []  # type: ignore[index]
    assert data["page"] == {"page": 1, "page_size": 50, "total": 0, "has_more": False}  # type: ignore[index]


def test_query_wildcard_chars_are_literal(conn: sqlite3.Connection) -> None:
    # A '%'/'_' in the query is escaped, so it matches a LITERAL char rather
    # than widening the filter to match-everything -- no display_name carries them.
    for wildcard in ("%", "_"):
        banners = _handler(conn)(server="en", query=wildcard).to_dict()["data"]["banners"]  # type: ignore[index]
        assert banners == [], wildcard


def test_query_composes_with_window_and_paging(conn: sqlite3.Connection) -> None:
    # The query narrows FIRST, then the since/until window + page apply over the
    # filtered set: "Headhunting" keeps all three, until drops LIMITED_1, page reports 2.
    env = _handler(conn)(
        server="en",
        query="Headhunting",
        until="2026-07-15T00:00:00+00:00",
        page={"page": 1, "page_size": 1},
    )
    data = env.to_dict()["data"]
    assert [b["game_id"] for b in data["banners"]] == ["CLASSIC_1"]  # type: ignore[index]
    assert data["page"] == {"page": 1, "page_size": 1, "total": 2, "has_more": True}  # type: ignore[index]


def test_query_stays_within_region(conn: sqlite3.Connection) -> None:
    # A cn query matches only cn banners; an en-only name returns nothing on cn.
    cn = _handler(conn)(server="cn", query="限定").to_dict()["data"]["banners"]  # type: ignore[index]
    assert [b["game_id"] for b in cn] == ["CN_1"]
    en_name_on_cn = _handler(conn)(server="cn", query="Headhunting").to_dict()["data"]["banners"]  # type: ignore[index]
    assert en_name_on_cn == []


def test_oversized_query_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", query="x" * (MAX_QUERY_LEN + 1))


def test_empty_query_rejected(conn: sqlite3.Connection) -> None:
    # min_length=1 (after whitespace strip): an empty/blank query is a malformed filter,
    # rejected at the model gate rather than degenerating into a match-nothing pattern.
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", query="")
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", query="   ")


# --- bounded pagination -------------------------------------------------------


def test_pagination_slices_and_reports_total(conn: sqlite3.Connection) -> None:
    env = _handler(conn)(server="en", page={"page": 1, "page_size": 2})
    data = env.to_dict()["data"]
    banners = data["banners"]  # type: ignore[index]
    page = data["page"]  # type: ignore[index]
    assert [b["game_id"] for b in banners] == ["LIMITED_1", "CLASSIC_1"]
    assert page == {"page": 1, "page_size": 2, "total": 3, "has_more": True}

    env2 = _handler(conn)(server="en", page={"page": 2, "page_size": 2})
    data2 = env2.to_dict()["data"]
    assert [b["game_id"] for b in data2["banners"]] == ["NORMAL_1"]  # type: ignore[index]
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
        get_banners(conn, server="en", page_size=101)
    with pytest.raises(ValueError, match="must be >= 1"):
        get_banners(conn, server="en", page=0)


# --- empty domain: ok empty list, never not_found -----------------------------


def test_empty_region_is_ok_empty_list(bare_conn: sqlite3.Connection) -> None:
    # gacha_table is tolerant-absent, so a region with no banners is a
    # legitimate empty ``ok`` list, not a ``not_found``.
    env = _handler(bare_conn)(server="en")
    assert env.status == "ok"
    data = env.to_dict()["data"]
    assert data["banners"] == []  # type: ignore[index]
    assert data["page"] == {"page": 1, "page_size": 50, "total": 0, "has_more": False}  # type: ignore[index]
    assert env.to_dict()["provenance"] == []
    assert env.limitations == ()


# --- fail-closed --------------------------------------------------------------


def test_database_unavailable_fails_closed() -> None:
    def boom() -> sqlite3.Connection:
        raise DatabaseUnavailable("database not found: /home/ubuntu/cand.sqlite")

    env = build_get_banners_spec(boom).handler(server="en")
    assert env.status == "database_unavailable"
    body = str(env.to_dict()["data"])
    assert "/home/ubuntu" not in body
    assert "Traceback" not in body


def test_internal_error_fails_closed() -> None:
    def boom() -> sqlite3.Connection:
        raise RuntimeError("unexpected: /secret/path")

    env = build_get_banners_spec(boom).handler(server="en")
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


def test_non_iso_since_rejected(conn: sqlite3.Connection) -> None:
    # A non-date bound is rejected at the model gate, never silently emptying
    # the windowed query lexicographically.
    with pytest.raises(ValidationError):
        _handler(conn)(server="en", since="july")


# --- shared registry + wire contract ------------------------------------------


def test_registered_in_shared_registry(conn: sqlite3.Connection) -> None:
    registry = build_tool_registry(
        lambda: conn, registry=load_source_registry(REGISTRY), mode="local"
    )
    assert "get_banners" in registry.names()
    spec = registry.get("get_banners")
    assert spec.read_only is True
    schema = spec.input_schema
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
