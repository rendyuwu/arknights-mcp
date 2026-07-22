"""T115: the M11 acceptance tests (§V62, §V5, §V30, §V16, §V7).

The milestone gate for M11 (banner archive). It drives the operator snapshot
fixtures through the *entire* M11 stack the way a CLI sync would -- the real
:func:`~arknights_mcp.importers.pipeline.build_candidate` pipeline, which imports
banners after operators (§T113) -- then reads the archive back through the
shared-core :func:`~arknights_mcp.services.banners.get_banners` both transports call
(§V14):

  build multi-region candidate (operator en + cn fixtures) -> import_banners runs
  inside the pipeline (gacha_table.json present for en, absent for cn) -> reopen
  read-only -> get_banners.

The banner archive is a metadata-only historical FACT (§V62), NOT planning: the en
``operator`` fixture's ``gacha_table.json`` carries a LIMITED banner (featured op under
``limitParam.limitedCharId``), a CLASSIC banner (an array under
``dynMeta.attainRare6CharList``), and a NORMAL standard banner (no typed featured-op),
all with prose (``gachaPoolSummary``/``gachaPoolDetail``/``rateUpHtml``) that must never
survive the allowlist. ``char_002_amiya`` is present as an en operator, so the LIMITED
and CLASSIC featured ops soft-resolve to its name. The cn fixture has no
``gacha_table.json`` at all, so a cn build legitimately yields zero banners -- the
region-separation and tolerant-absent cases fall straight out of the real pipeline.

Unlike the per-task unit tests (which seed banners via ``insert_banners`` directly),
this asserts the whole M11 story end to end:

* **banner metadata, no prose** (§V62/§V16): each banner carries only the typed
  schedule/identity fields + typed featured ops; gacha prose survives into neither the
  built DB nor the served result.
* **typed featured-op per rule type** (§V62): LIMITED resolves ``limitedCharId`` to the
  present operator, CLASSIC resolves the ``attainRare6CharList`` array, NORMAL carries no
  typed featured-op and surfaces the standard-banner limitation (§V26).
* **region integrity** (§V5): en banners are never surfaced under a cn query; every
  banner + its provenance is region-tagged, en & cn never silently mixed.
* **fail-closed vs tolerant-absent** (§V30/B36): a cn snapshot without ``gacha_table``
  promotes with zero banners (legitimate empty), but a non-empty ``gachaPoolClient`` that
  resolves to zero banners fails the whole build closed.
* **since/until window** (§V19): the optional open-time bounds narrow the list.
* **no new source** (§V62): the archive reuses ``arknights_assets_gamedata``;
  ``get_data_sources`` grows no banner/gacha source.
* **archive is a FACT, not planning** (§V7): no mandatory/best/pity/spark verdict leaks.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.services.banners import (
    STANDARD_BANNER_LIMITATION,
    BannersResult,
    get_banners,
)
from arknights_mcp.services.source_status import get_data_sources
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATOR_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "operator"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: Pinned so the banner provenance (snapshot_id + imported_at) is byte-stable.
PINNED_IMPORTED_AT = "2026-07-18T00:00:00+00:00"

#: The three en banners the fixture gacha_table carries, newest-first by open_time.
_LIMITED = "LIMITED_TEST_1"  # 2023-11-14, featured op char_002_amiya
_CLASSIC = "CLASSIC_TEST_1"  # 2023-12-08, featured op char_002_amiya
_NORMAL = "NORMAL_TEST_1"  # 2023-12-31, no typed featured-op

#: Open-time bounds straddling the three banners (ISO compares chronologically here):
#: since keeps CLASSIC+NORMAL (drops LIMITED); until keeps LIMITED+CLASSIC (drops NORMAL).
_SINCE = "2023-12-01T00:00:00+00:00"
_UNTIL = "2023-12-15T00:00:00+00:00"

#: A prose sentinel shared by every prose field in the fixture gacha_table
#: (gachaPoolSummary/gachaPoolDetail/rateUpHtml). A correct metadata-only pipeline drops
#: it, so it appears in neither the built DB nor the served result (§V16/§V62).
_PROSE = "must never be imported"

#: Planning / prescriptive language a banner ARCHIVE must never emit -- it is a
#: historical FACT, not gacha planning (§V7/§V62). None collides with the legitimate
#: "rate-up not in typed gamedata" limitation wording.
_PROSCRIBED = ("pity", "spark", "pull probability", "guaranteed", "best banner", "mandatory")


def _adapter(root: Path, server: str) -> LocalSnapshotAdapter:
    return LocalSnapshotAdapter(root, server, "local_snapshot")


def _build(tmp_path: Path) -> Path:
    """Build the multi-region operator candidate the way a CLI sync would (§T21/§T22).

    The en fixture carries a gacha_table.json (LIMITED/CLASSIC/NORMAL + prose); the cn
    fixture has none. import_banners runs inside the pipeline after operators, so the
    whole banner path (allowlist -> soft-resolve -> DB) is exercised, not stubbed.
    """
    path = tmp_path / "cand.sqlite"
    build_candidate(
        path,
        [
            ServerImport("en", _adapter(OPERATOR_FIXTURES / "en", "en"), "local_snapshot"),
            ServerImport("cn", _adapter(OPERATOR_FIXTURES / "cn", "cn"), "local_snapshot"),
        ],
        registry=load_source_registry(REGISTRY),
        imported_at=PINNED_IMPORTED_AT,
    )
    return path


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return open_read_only(_build(tmp_path))


def _by_id(result: BannersResult) -> dict[str, object]:
    return {b.game_id: b for b in result.banners}


# --- banner metadata rows, no prose (§V62/§V16) -------------------------------


def test_accept_synced_db_has_banner_metadata_rows(conn: sqlite3.Connection) -> None:
    # §V62: a synced db surfaces the en banner archive -- the three fixture pools,
    # newest-first, each region-tagged en with a pinned banner provenance chain (§V5).
    result = get_banners(conn, server="en")
    assert result.status == "ok"
    assert result.server == "en"
    assert [b.game_id for b in result.banners] == [_NORMAL, _CLASSIC, _LIMITED]
    assert all(b.region == "en" for b in result.banners)
    # §V5/§V17: one en banner snapshot backs the set, region-tagged + pinned.
    assert len(result.provenance) == 1
    assert result.provenance[0].snapshot_id.startswith("en:")
    assert result.provenance[0].imported_at == PINNED_IMPORTED_AT


def test_accept_no_gacha_prose_survives(conn: sqlite3.Connection) -> None:
    # §V16/§V62: gacha prose (summary/detail/html) survives into neither the served
    # result nor the built DB -- the metadata-only ceiling holds through the full pipeline.
    result = get_banners(conn, server="en")
    served = json.dumps(asdict(result), ensure_ascii=True)
    assert _PROSE not in served
    for forbidden in ("gachaPoolSummary", "gachaPoolDetail", "rateUpHtml", "dynMeta"):
        assert forbidden not in served

    tables = [
        name
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    db_dump = "\n".join(
        str(row) for table in tables for row in conn.execute(f"SELECT * FROM {table}")
    )
    assert _PROSE not in db_dump
    assert "gachaPoolSummary" not in db_dump and "rateUpHtml" not in db_dump


def test_accept_banner_wire_keys_are_metadata_only(conn: sqlite3.Connection) -> None:
    # §V62: exactly the typed schedule/identity fields + typed featured ops reach the
    # wire; there is no gacha prose field for a banner to carry.
    allowed = {
        "game_id",
        "display_name",
        "open_time",
        "end_time",
        "rule_type",
        "region",
        "featured_ops",
    }
    op_keys = {"char_id", "resolved", "operator_name"}
    for banner in asdict(get_banners(conn, server="en"))["banners"]:
        assert set(banner) == allowed
        for op in banner["featured_ops"]:
            assert set(op) == op_keys


# --- typed featured-op per rule type (§V62/§V26) ------------------------------


def test_accept_limited_featured_op_resolves_to_operator(conn: sqlite3.Connection) -> None:
    # §V62: a LIMITED banner names one featured op under limitParam.limitedCharId, which
    # soft-resolves to the present en operator's name.
    limited = _by_id(get_banners(conn, server="en"))[_LIMITED]
    ops = limited.featured_ops  # type: ignore[attr-defined]
    assert [(o.char_id, o.resolved, o.operator_name) for o in ops] == [
        ("char_002_amiya", True, "Amiya")
    ]


def test_accept_classic_family_reads_attain_rare6_list(conn: sqlite3.Connection) -> None:
    # §V62: a CLASSIC-family banner names its featured ops via dynMeta.attainRare6CharList.
    classic = _by_id(get_banners(conn, server="en"))[_CLASSIC]
    ops = classic.featured_ops  # type: ignore[attr-defined]
    assert [(o.char_id, o.resolved, o.operator_name) for o in ops] == [
        ("char_002_amiya", True, "Amiya")
    ]


def test_accept_normal_has_no_featured_op_and_limitation(conn: sqlite3.Connection) -> None:
    # §V62/§V26: a NORMAL standard banner carries no typed featured-op (its rate-up is
    # prose only, §V18-forbidden) -> none emitted + the standard-banner caveat surfaced.
    result = get_banners(conn, server="en")
    normal = _by_id(result)[_NORMAL]
    assert normal.featured_ops == ()  # type: ignore[attr-defined]
    assert STANDARD_BANNER_LIMITATION in result.limitations


# --- region integrity (§V5) ---------------------------------------------------


def test_accept_regions_never_silently_mixed(conn: sqlite3.Connection) -> None:
    # §V5: the cn fixture has no gacha_table, so a cn query returns zero banners; the en
    # banners are never surfaced under it, and the en set is entirely en-tagged.
    en = get_banners(conn, server="en")
    assert en.banners and all(b.region == "en" for b in en.banners)
    assert all(p.snapshot_id.startswith("en:") for p in en.provenance)

    cn = get_banners(conn, server="cn")
    assert cn.status == "ok"
    assert cn.banners == ()
    assert cn.provenance == ()


# --- fail-closed vs tolerant-absent (§V30/B36) --------------------------------


def test_accept_absent_gacha_table_promotes_empty(conn: sqlite3.Connection) -> None:
    # §V30/B36: the cn snapshot has no gacha_table.json, so the build promotes with zero
    # banners rather than failing -- the banner domain is optional per snapshot. That the
    # ``conn`` fixture opened at all proves the multi-region build promoted.
    assert get_banners(conn, server="cn").banners == ()


def test_accept_non_empty_gacha_yielding_zero_fails_closed(tmp_path: Path) -> None:
    # §V30: a non-empty gachaPoolClient whose entries all lack a gachaPoolId resolves to
    # zero banners -> the whole candidate build fails closed (a shape/id mismatch is never
    # a silently promoted empty banner build); the active DB stays untouched (§V3).
    snap = tmp_path / "en"
    shutil.copytree(OPERATOR_FIXTURES / "en", snap)
    idless = {"gachaPoolClient": [{"gachaRuleType": "NORMAL"}, {"gachaPoolName": "x"}]}
    (snap / "gamedata" / "excel" / "gacha_table.json").write_text(
        json.dumps(idless), encoding="utf-8"
    )
    with pytest.raises(ImporterError, match="silent empty banner build"):
        build_candidate(
            tmp_path / "cand.sqlite",
            [ServerImport("en", _adapter(snap, "en"), "local_snapshot")],
            registry=load_source_registry(REGISTRY),
            imported_at=PINNED_IMPORTED_AT,
        )


# --- since/until open-time window (§V19) --------------------------------------


def test_accept_since_until_filter(conn: sqlite3.Connection) -> None:
    since = get_banners(conn, server="en", since=_SINCE)
    assert [b.game_id for b in since.banners] == [_NORMAL, _CLASSIC]

    until = get_banners(conn, server="en", until=_UNTIL)
    assert [b.game_id for b in until.banners] == [_CLASSIC, _LIMITED]

    window = get_banners(conn, server="en", since=_SINCE, until=_UNTIL)
    assert [b.game_id for b in window.banners] == [_CLASSIC]


# --- no new source (§V62) -----------------------------------------------------


def test_accept_get_data_sources_unchanged_no_banner_source(conn: sqlite3.Connection) -> None:
    # §V62: the banner archive reuses arknights_assets_gamedata -- no new source id is
    # registered for it, so get_data_sources grows no banner/gacha entry.
    registry = load_source_registry(REGISTRY)
    ids = {s.source_id for s in get_data_sources(registry, conn).sources}
    assert "arknights_assets_gamedata" in ids
    assert not any("banner" in sid or "gacha" in sid for sid in ids)


# --- archive is a FACT, not planning (§V7) ------------------------------------


def test_accept_no_planning_verdict_leaks(conn: sqlite3.Connection) -> None:
    # §V7/§V62: a banner archive is a historical FACT, never gacha planning -- no
    # mandatory/best/pity/spark language rides the served result or its limitations.
    result = get_banners(conn, server="en")
    blob = (json.dumps(asdict(result)) + " " + " ".join(result.limitations)).lower()
    assert not any(word in blob for word in _PROSCRIBED)
