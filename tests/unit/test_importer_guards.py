"""T189/B125: the shared importer guards have exactly one home (§V37/§V30/§V58).

Two patterns had been copy-pasted per domain; both now live in
:mod:`arknights_mcp.importers.guards`:

* the §V30 silent-empty guard -- six former inline copies (banners, skins, penguin
  drops, announcements twice, activity titles) plus the pipeline's combat guard;
* the §V58 optional-domain fail-open -- two verbatim pipeline copies (banners, and
  skins copy-pasted from banners).

This verifies the shared behaviour AND that no divergent copy remains in the modules
that used to carry one -- the introspection half is the part that stops the B92/B106
copy-drift class from re-landing with the next optional domain.
"""

from __future__ import annotations

import inspect
import logging
import sqlite3

import pytest

from arknights_mcp.importers import announcements as announcements_mod
from arknights_mcp.importers import banners as banners_mod
from arknights_mcp.importers import guards as guards_mod
from arknights_mcp.importers import penguin_drops as penguin_mod
from arknights_mcp.importers import pipeline as pipeline_mod
from arknights_mcp.importers import skins as skins_mod
from arknights_mcp.importers import stages as stages_mod
from arknights_mcp.importers.enemies import ImporterError
from arknights_mcp.importers.guards import (
    guard_not_silently_empty,
    import_optional_domain,
    refuse_silent_empty,
)

_GUARD_KW = {
    "source": "gacha_table",
    "unit": "pool entr(y|ies)",
    "resolution": "resolved to a banner",
    "outcome": "empty banner build",
}


# --- §V30: the shared silent-empty predicate ---------------------------------------


def test_candidates_with_zero_produced_fails_closed() -> None:
    with pytest.raises(ImporterError) as exc_info:
        guard_not_silently_empty(candidates=3, produced=0, scope="en", **_GUARD_KW)
    assert str(exc_info.value) == (
        "en: gacha_table had 3 pool entr(y|ies) but none resolved to a banner; "
        "refusing a silent empty banner build (§V30)"
    )


def test_empty_source_is_a_legitimate_empty_domain() -> None:
    # Zero candidates is not a regression: an optional domain the snapshot simply
    # does not carry imports zero rows without failing the build (B36/§V41).
    guard_not_silently_empty(candidates=0, produced=0, scope="en", **_GUARD_KW)


def test_any_produced_row_passes() -> None:
    guard_not_silently_empty(candidates=3, produced=1, scope="en", **_GUARD_KW)


def test_scope_is_optional_for_parse_time_callers() -> None:
    # parse_activity_titles knows no region, so the region prefix is omitted rather
    # than emitted as a "None: " placeholder.
    with pytest.raises(ImporterError) as exc_info:
        guard_not_silently_empty(candidates=26, produced=0, **_GUARD_KW)
    assert str(exc_info.value).startswith("gacha_table had 26 ")


def test_compound_cite_and_detail_ride_through() -> None:
    with pytest.raises(ImporterError) as exc_info:
        guard_not_silently_empty(
            candidates=2,
            produced=0,
            scope="cn",
            source="announcement feed",
            unit="entr(y|ies)",
            resolution="carried a mapped date",
            outcome="degraded announcement build",
            cite="§V30/§V61",
            detail=" -- the feed field-map matched no known date shape",
        )
    assert str(exc_info.value) == (
        "cn: announcement feed had 2 entr(y|ies) but none carried a mapped date; "
        "refusing a silent degraded announcement build (§V30/§V61) -- the feed "
        "field-map matched no known date shape"
    )


def test_refuse_silent_empty_always_raises_typed() -> None:
    # §V33: the low-level home for the raise, used by the one caller whose reason does
    # not fit the candidates-vs-produced predicate (the pipeline's tiles/spawns case).
    with pytest.raises(ImporterError, match=r"^en: boom; refusing a silent empty combat build"):
        refuse_silent_empty("en: boom", outcome="empty combat build")


# --- §V58: the shared optional-domain fail-open ------------------------------------


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (k INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO t (k) VALUES (1)")
    return conn


def _rows(conn: sqlite3.Connection) -> list[int]:
    return [row[0] for row in conn.execute("SELECT k FROM t ORDER BY k")]


def test_optional_domain_returns_the_import_result_on_success() -> None:
    conn = _conn()
    try:
        result = import_optional_domain(
            conn,
            lambda: "imported",
            domain="banners",
            server="en",
            describe="banner archive",
            empty=lambda: "empty",
            cites="§V62/§V58",
        )
        assert result == "imported"
    finally:
        conn.close()


def test_optional_domain_rolls_back_partial_rows_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # §V58/§V3: the domain's ImporterError rolls back only THIS domain's writes (the
    # row written before the block survives), the build continues with the typed empty
    # result, and the skip is warned once so it is visible in the sync log.
    conn = _conn()
    try:

        def _failing() -> str:
            conn.execute("INSERT INTO t (k) VALUES (2)")
            raise ImporterError("gacha_table had 3 pool entr(y|ies) but none resolved")

        with caplog.at_level(logging.WARNING):
            result = import_optional_domain(
                conn,
                _failing,
                domain="banners",
                server="en",
                describe="banner archive",
                empty=lambda: "empty",
                cites="§V62/§V58",
            )
        assert result == "empty"
        assert _rows(conn) == [1]
        assert any(
            "en: banner archive unavailable, skipped; continuing combat build (§V62/§V58)"
            in r.getMessage()
            for r in caplog.records
        )
    finally:
        conn.close()


def test_optional_domain_does_not_swallow_other_errors() -> None:
    # Fail-open is bounded to the typed ImporterError: a programming error still tears
    # the build down rather than shipping a domain-less build under a warning.
    conn = _conn()

    def _boom() -> str:
        conn.execute("INSERT INTO t (k) VALUES (2)")
        raise RuntimeError("bug")

    try:
        with pytest.raises(RuntimeError, match="bug"):
            import_optional_domain(
                conn,
                _boom,
                domain="banners",
                server="en",
                describe="banner archive",
                empty=lambda: "empty",
                cites="§V62/§V58",
            )
        # the savepoint still rolled the partial write back before re-raising
        assert _rows(conn) == [1]
    finally:
        conn.close()


# --- §V37: no divergent copies remain ----------------------------------------------


def test_silent_empty_message_has_single_home() -> None:
    # V37: only the guards module builds the refusal message; every former copy now
    # calls the shared guard instead of re-forking the f-string.
    for mod in (
        banners_mod,
        skins_mod,
        penguin_mod,
        announcements_mod,
        stages_mod,
        pipeline_mod,
    ):
        src = inspect.getsource(mod)
        assert "refusing a silent" not in src, mod.__name__
        assert "guard_not_silently_empty" in src or "refuse_silent_empty" in src, mod.__name__
    assert guard_not_silently_empty.__module__ == "arknights_mcp.importers.guards"


def test_fail_open_block_has_single_home() -> None:
    # V37/B125: the savepoint + `except ImporterError` + warn + empty-result block is
    # gone from the pipeline; the next optional domain gets the helper, not a copy.
    src = inspect.getsource(pipeline_mod)
    assert "except ImporterError" not in src
    assert "savepoint(" not in src
    assert src.count("import_optional_domain(") == 2
    assert import_optional_domain.__module__ == "arknights_mcp.importers.guards"


def test_guards_module_holds_both_patterns_once() -> None:
    # Counts the RAISE and CATCH sites, not prose: the docstrings quote the message
    # template, so a substring count over the whole source would pass on a doc mention.
    src = inspect.getsource(guards_mod)
    assert src.count("raise ImporterError(") == 1
    assert src.count("except ImporterError") == 1
