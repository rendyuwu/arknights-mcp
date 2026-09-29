"""Since/until bound-RELATION guard.

The bound SHAPE was already typed (a non-ISO ``since`` is rejected at the model gate);
this guard types the RELATION between the pair, the other half of the same
one-check-not-both defect. Verified live on the promoted build before the fix: the pair
``since=2026-07-01, until=2026-06-01`` returned ``ok`` + an empty collection on BOTH tools
-- with a "widen or drop the bounds" limitation on ``get_announcements`` (false advice:
widening cannot help an inverted pair) and with NO limitation at all on ``get_banners``,
i.e. indistinguishable from an empty archive.

These drive the three enforcement points:

* the predicate itself -- the comparison the window really performs, because both
  repositories filter a TEXT column with ``>= :since`` / ``<= :until || '~'``, so string
  order IS the window's order. A test pins the predicate to that comparison, sentinel
  included: later work showed both halves of that mirroring matter, since a bare
  ``since > until`` rejected intra-day windows the SQL answers;
* the model gate on both windowed inputs -> a typed ``invalid_input`` envelope through the
  shared dispatch home, never a leaked pydantic error;
* the service mirror -- rejected at BOTH the model and the service, one contract in both
  places, exactly as the page bounds are.

An equal pair is a legitimate single-day window and a one-sided bound is legitimate too,
so both must survive: a guard that over-rejects would withhold real rows, which is the
one thing this rejection is safe from (an inverted pair can match nothing by the total
ordering of strings).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.importers.pipeline import ServerImport, build_candidate
from arknights_mcp.mcp.tool_registry import MAX_TOOL_DESCRIPTION_CHARS, ToolRegistry
from arknights_mcp.mcp.tools import build_tool_registry
from arknights_mcp.mcp.tools.announcements import _TOOL_DESCRIPTION as ANNOUNCEMENTS_DESCRIPTION
from arknights_mcp.mcp.tools.banners import _TOOL_DESCRIPTION as BANNERS_DESCRIPTION
from arknights_mcp.models.announcements import GetAnnouncementsInput
from arknights_mcp.models.banners import GetBannersInput
from arknights_mcp.models.common import inverted_window_reason, reject_inverted_window
from arknights_mcp.services.announcements import get_announcements
from arknights_mcp.services.banners import get_banners
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter
from arknights_mcp.sources.registry import load_source_registry
from arknights_mcp.transports._server import dispatch_tool_call
from arknights_mcp.util.iso_bounds import UNTIL_UPPER_SENTINEL, canonical_iso_bound

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: The two windowed tool inputs (one validator, every windowed surface).
WINDOWED_MODELS = (GetAnnouncementsInput, GetBannersInput)

#: The two windowed tools, and the services behind them.
WINDOWED_TOOLS = ("get_announcements", "get_banners")
WINDOWED_SERVICES = (get_announcements, get_banners)

#: The originally filed pair.
INVERTED = ("2026-07-01", "2026-06-01")

#: Chronologically ASCENDING, yet inverted in the TEXT order the old guard compared in:
#: ``fromisoformat`` accepts ISO basic format, so this passes the shape gate while
#: ``"20260801" > "2026-09-01"`` (``-`` sorts below a digit). The old guard rejected it,
#: naming the notation. The render handles both bounds instead, so the pair now ANSWERS --
#: its instants were never contradictory, and a rejection that withholds a real answer is
#: the thing the rejection arm may not do. It stays here as exactly that control.
MIXED_FORM = ("20260801", "2026-09-01")

#: Windows that must SURVIVE: a single-day window, each one-sided bound, an ascending
#: pair, and no window at all. Over-rejecting withholds real rows.
VALID_WINDOWS = (
    ("2026-06-01", "2026-06-01"),
    ("2026-06-01", None),
    (None, "2026-06-01"),
    ("2026-01-01", "2026-02-01"),
    (None, None),
    ("2026-06-01T08:00:00+00:00", "2026-06-01T09:00:00+00:00"),
)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """A promoted-shape 4-4 build. The guard rejects BEFORE any query, so the optional
    announcement/banner domains need no seeding -- what matters is that a real read-only
    connection is on the production path."""
    path = tmp_path / "candidate.sqlite"
    build_candidate(
        path,
        [
            ServerImport(
                "en", LocalSnapshotAdapter(FIXTURE_ROOT, "en", "local_snapshot"), "local_snapshot"
            )
        ],
        registry=load_source_registry(REGISTRY),
    )
    return open_read_only(path)


@pytest.fixture
def registry(conn: sqlite3.Connection) -> ToolRegistry:
    return build_tool_registry(lambda: conn, registry=load_source_registry(REGISTRY), mode="local")


# --- the predicate -------------------------------------------------------------


@pytest.mark.parametrize(("since", "until"), VALID_WINDOWS)
def test_a_window_that_can_match_is_not_rejected(since: str | None, until: str | None) -> None:
    assert inverted_window_reason(since, until) is None
    reject_inverted_window(since, until)


def test_inverted_pair_reason_names_both_bounds_and_the_swap() -> None:
    reason = inverted_window_reason(*INVERTED)
    assert reason is not None
    assert INVERTED[0] in reason and INVERTED[1] in reason
    assert "swap" in reason


def test_mixed_iso_forms_are_normalized_and_answered_not_rejected() -> None:
    # The old message is superseded: the pair is chronologically ASCENDING,
    # so both bounds are rendered to one notation and the window is answered. Rejecting it
    # would have withheld an answer the caller was entitled to; "since is later than until"
    # would have been false about the input either way.
    assert inverted_window_reason(*MIXED_FORM) is None
    reject_inverted_window(*MIXED_FORM)


@pytest.mark.parametrize(
    ("since", "until"),
    [
        *VALID_WINDOWS,
        INVERTED,
        MIXED_FORM,
        ("2026-06-01", "20260701"),
        ("2026-06-01T09:00:00", "2026-06-01T08:00:00"),
        ("2026-06-01T09:00:00", "2026-06-01"),
    ],
)
def test_predicate_is_the_comparison_the_window_performs(
    since: str | None, until: str | None
) -> None:
    # The repositories filter a TEXT column (``>= :since`` / ``<= :until || '~'``) over
    # CANONICAL bound text, so the rejection must fire on exactly the pairs whose
    # order in that comparison makes a match impossible -- no wider (that withholds rows,
    # the first arm) and no narrower (that re-admits the inversion).
    impossible = (
        since is not None
        and until is not None
        and canonical_iso_bound(since) > canonical_iso_bound(until) + UNTIL_UPPER_SENTINEL
    )
    assert (inverted_window_reason(since, until) is not None) is impossible


def test_reason_carries_no_internal_cite_or_jargon() -> None:
    # The reason reaches a client verbatim inside the invalid_input envelope.
    reason = inverted_window_reason(*INVERTED)
    assert reason is not None
    for token in ("lexicograph", "degenerate"):
        assert token not in reason


# --- the model gate on every windowed input -----------------------------------


@pytest.mark.parametrize("model", WINDOWED_MODELS)
@pytest.mark.parametrize(("since", "until"), [INVERTED])
def test_model_gate_rejects_an_impossible_window(
    model: type[GetAnnouncementsInput] | type[GetBannersInput],
    since: str,
    until: str,
) -> None:
    with pytest.raises(ValidationError):
        model(server="en", since=since, until=until)


@pytest.mark.parametrize("model", WINDOWED_MODELS)
@pytest.mark.parametrize(("since", "until"), VALID_WINDOWS)
def test_model_gate_accepts_a_window_that_can_match(
    model: type[GetAnnouncementsInput] | type[GetBannersInput],
    since: str | None,
    until: str | None,
) -> None:
    parsed = model(server="en", since=since, until=until)
    assert (parsed.since, parsed.until) == (since, until)


# --- the service mirror --------------------------------------------------------


@pytest.mark.parametrize("service", WINDOWED_SERVICES)
@pytest.mark.parametrize(("since", "until"), [INVERTED])
def test_service_rejects_an_impossible_window(
    conn: sqlite3.Connection, service: object, since: str, until: str
) -> None:
    # A caller reaching the service directly (bypassing the model gate) gets the same
    # rejection, never a silent empty -- exactly how the page bounds behave.
    with pytest.raises(ValueError, match="can match nothing"):
        service(conn, server="en", since=since, until=until)  # type: ignore[operator]


@pytest.mark.parametrize("service", WINDOWED_SERVICES)
def test_service_still_answers_a_single_day_window(
    conn: sqlite3.Connection, service: object
) -> None:
    result = service(conn, server="en", since="2026-06-01", until="2026-06-01")  # type: ignore[operator]
    assert result.status == "ok"


# --- the wire: a typed invalid_input, not a leaked framework error -------------


@pytest.mark.parametrize("tool", WINDOWED_TOOLS)
def test_dispatch_delivers_a_typed_invalid_input(registry: ToolRegistry, tool: str) -> None:
    env = dispatch_tool_call(
        registry, tool, {"server": "en", "since": INVERTED[0], "until": INVERTED[1]}
    )
    assert env.status == "invalid_input"
    body = env.to_dict()["data"]
    assert isinstance(body, dict)
    message = str(body["message"])
    # A model-level validator reports no field ``loc``, so the message must name the two
    # offending values itself for the client to know what to fix.
    assert INVERTED[0] in message and INVERTED[1] in message
    assert body["suggested_action"]
    serialized = str(env.to_dict())
    assert "errors.pydantic.dev" not in serialized
    assert "validation error" not in serialized.lower()


@pytest.mark.parametrize("tool", WINDOWED_TOOLS)
def test_dispatch_still_answers_a_single_day_window(registry: ToolRegistry, tool: str) -> None:
    # The negative half of the guard: the same path with a legitimate window is an ``ok``
    # answer, so a passing rejection test cannot be satisfied by rejecting everything.
    env = dispatch_tool_call(
        registry, tool, {"server": "en", "since": "2026-06-01", "until": "2026-06-01"}
    )
    assert env.status == "ok"


# --- the description states the constraint, within budget -----------------------


@pytest.mark.parametrize(
    "description", [ANNOUNCEMENTS_DESCRIPTION, BANNERS_DESCRIPTION], ids=WINDOWED_TOOLS
)
def test_description_states_the_bound_relation_within_budget(description: str) -> None:
    assert "since after until" in description
    assert "YYYY-MM-DD" in description
    assert len(description) <= MAX_TOOL_DESCRIPTION_CHARS
