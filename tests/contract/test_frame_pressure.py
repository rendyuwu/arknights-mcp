"""The tripwire -- every tool, widest legal request, whole build.

Arms (a) and (b) fixed two shapes. This is the guard that says a third one cannot ship
quietly. Two earlier defects were the same failure one tool apart, and both root causes are
recorded in the same words: *no test drove a tool at its widest legal request over the real
corpus*. The cap suite drove defaults, so a frame that only overruns with every include
flag on and ``page_size=100`` was outside everything anyone was looking at.

So this module drives exactly that shape -- derived from each tool's own published schema
(:mod:`arknights_mcp.mcp.cap_pressure`), never from a list of parameters somebody
maintains -- over every row of the promoted build, and reads the frame **before** any shed.
Four things then have to hold:

* the measured peak sits inside the band its declaration pinned (the distribution half,
  and the part that fires when a new event grows a fatter window);
* the set of tools that exceed the cap is *exactly* the set declaring ``ShedStatus.LIVE``,
  in both directions;
* every tool that exceeds it answers -- non-empty ``data`` under the cap -- which is the
  harm itself, checked as behaviour rather than as the presence of a
  ``shed_plan=`` argument, so the declaration cannot drift from the code;
* every tool that fits is byte-identical with the cap lifted, so nothing sheds spuriously;
* the two ways of fitting stay distinguishable BY EXECUTION: a ``DEAD_TODAY`` plan
  still fires when the cap is lowered under the tool's own peak, and a ``NONE`` tool still
  withholds when it is. Without that pair, ``dead_today`` would be a place to park a
  declaration for a plan somebody had already deleted -- a guard that cannot fail --
  and the lowered cap is the synthetic value a proof requires, because the promoted build
  no longer reaches the arm.

Skipped without a promoted build. Not added to ``ci.yml``'s enumerated module list on
purpose: that list names the modules the live-upstream job guards and this needs no network
(same precedent as the other arms); ``ci.yml`` already runs the whole suite. The sweep is
the expensive kind of honest -- about 75 seconds, three quarters of it ``get_stage`` at max
detail over 6716 stages -- and it runs once per session as a module fixture.
"""

from __future__ import annotations

import contextlib
import itertools
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import ValidationError
from tests.support.tool_calls import active_build, registry_for

from arknights_mcp.db.connection import open_read_only
from arknights_mcp.mcp import envelopes
from arknights_mcp.mcp.cap_pressure import (
    CREEP_CEILING,
    FRAME_PRESSURE,
    PRESSURE_BY_TOOL,
    STALE_FLOOR,
    InputKind,
    ShedStatus,
    ToolFramePressure,
    classify_inputs,
    enum_filter_variants,
    widest_knobs,
)
from arknights_mcp.mcp.envelopes import MAX_RESPONSE_BYTES, ResponseEnvelope, wire_size
from arknights_mcp.mcp.tool_registry import ToolRegistry, ToolSpec
from arknights_mcp.models.operators import FIND_FILTER_REQUIRED

BUILD = active_build()

pytestmark = pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)

#: The row-addressed tools and the corpus each one is swept over. Ordered so a peak is
#: reproducible: the sweep reports the FIRST row reaching the maximum.
_ROW_CORPUS: Mapping[str, str] = {
    "get_stage": "select server, game_id from stages order by server, game_id",
    "get_enemy": "select server, game_id from enemies order by server, game_id",
    "get_operator": "select server, game_id from operators order by server, game_id",
    "compare_operator_modules": "select server, game_id from operators order by server, game_id",
    "analyze_stage": "select server, game_id from stages order by server, game_id",
    "get_stage_drops": "select server, game_id from stages order by server, game_id",
    "get_item_drops": "select server, game_id from items order by server, game_id",
    "get_my_operator": "select server, game_id from operators order by server, game_id",
}

#: Region-scoped list tools: swept page by page to the end of both regions.
_PAGED_TOOLS = (
    "get_announcements",
    "get_banners",
    "find_operators",
    "get_my_roster",
    "get_my_inventory",
)

#: Free-text tools. Their request space is unbounded, so the battery below stands in for it
#: and the declaration says so -- what actually bounds them is 50 rows of capped locator
#: fields, not the query.
_SEARCH_TOOLS = ("search_entities", "search_stages")

#: No client-controlled input at all: one shape each, which IS the widest legal request.
_STATIC_TOOLS = ("get_data_status", "get_data_sources")

#: Guard against a paged sweep spinning on a malformed ``has_more``.
_MAX_PAGES = 500

#: The lifted cap. Large enough that no real payload reaches it, so a measurement taken
#: under the lift is the pre-shed frame.
_NO_CAP = 10**9


@dataclass(frozen=True)
class Peak:
    """The heaviest frame one tool produced, and the call that produced it."""

    frame: int = 0
    label: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@contextlib.contextmanager
def _cap_lifted() -> Iterator[None]:
    """Read the PRE-shed frame, the way the earlier count did.

    ``MAX_RESPONSE_BYTES`` is referenced only inside ``envelopes`` (``_enforce_cap`` and
    the cap limitation), so rebinding the module attribute lifts it everywhere. That the
    lift is total is not assumed -- ``test_the_cap_lift_is_total`` proves it, because a lift
    that silently failed would turn every "pre-shed" figure below into a post-shed one and
    the whole sweep would read comfortable.
    """
    original = envelopes.MAX_RESPONSE_BYTES
    envelopes.MAX_RESPONSE_BYTES = _NO_CAP
    try:
        yield
    finally:
        envelopes.MAX_RESPONSE_BYTES = original


@pytest.fixture(scope="module")
def conn() -> Iterator[sqlite3.Connection]:
    assert BUILD is not None
    with open_read_only(BUILD) as connection:
        yield connection


@pytest.fixture(scope="module")
def registry(conn: sqlite3.Connection) -> ToolRegistry:
    return registry_for(conn)


def _query_battery(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Deterministic stand-in for the free-text request space.

    Single characters saturate the 50-row window (the promoted build's peak is ``'e'``), and
    the longest display names in each entity table reach the fattest individual rows.
    """
    battery = list("abcdefghijklmnopqrstuvwxyz0123456789")
    for table in ("operators", "enemies", "stages", "items"):
        battery += [
            str(row[0])
            for row in conn.execute(
                f"select display_name from {table} where display_name is not null "  # noqa: S608
                "order by length(display_name) desc, display_name limit 25"
            )
        ]
    return tuple(battery)


def _better(best: Peak, env: ResponseEnvelope, label: str, params: dict[str, Any]) -> Peak:
    frame = wire_size(env)
    return Peak(frame, label, params) if frame > best.frame else best


def _sweep_row_tool(spec: ToolSpec, conn: sqlite3.Connection, sql: str) -> Peak:
    pressure = PRESSURE_BY_TOOL[spec.name]
    knobs = widest_knobs(spec.input_schema, pressure)
    variants = enum_filter_variants(spec.input_schema, pressure)
    best = Peak()
    for server, game_id in conn.execute(sql):
        for variant in variants:
            params = {"server": server, "game_id": game_id, **knobs, **variant}
            best = _better(best, spec.handler(**params), f"{server}/{game_id}", params)
    return best


def _page_info(payload: Mapping[str, object]) -> Mapping[str, Any] | None:
    for value in payload.values():
        if isinstance(value, Mapping) and "has_more" in value:
            return value
    return None


def _sweep_paged_tool(spec: ToolSpec) -> Peak:
    pressure = PRESSURE_BY_TOOL[spec.name]
    knobs = widest_knobs(spec.input_schema, pressure)
    kinds = classify_inputs(spec.input_schema, pressure)
    page_key = next(name for name, kind in kinds.items() if kind is InputKind.PAGE)
    variants = enum_filter_variants(spec.input_schema, pressure)
    best = Peak()
    for server, variant in itertools.product(("en", "cn"), variants):
        # Sorted so the label a peak reports is stable enough to pin in the declaration.
        filters = "".join(f", {name}={value}" for name, value in sorted(variant.items()))
        for page in range(1, _MAX_PAGES + 1):
            params = {
                "server": server,
                **knobs,
                **variant,
                page_key: {**knobs[page_key], "page": page},
            }
            try:
                env = spec.handler(**params)
            except ValidationError as exc:
                # The one shape outside the legal request space: find_operators with no
                # narrowing filter. Anything else is a regression, so it surfaces.
                if page > 1 or variant or FIND_FILTER_REQUIRED not in str(exc):
                    raise
                break
            size = params[page_key]["page_size"]
            best = _better(best, env, f"{server} page {page}, page_size={size}{filters}", params)
            info = _page_info(env.data)
            if info is None or not info.get("has_more"):
                break
    return best


def _sweep_search_tool(spec: ToolSpec, battery: tuple[str, ...]) -> Peak:
    pressure = PRESSURE_BY_TOOL[spec.name]
    knobs = widest_knobs(spec.input_schema, pressure)
    variants = enum_filter_variants(spec.input_schema, pressure)
    best = Peak()
    for query in battery:
        for variant in variants:
            params = {"query": query, **knobs, **variant}
            # Sorted so the label a peak reports is stable enough to pin in the declaration.
            filters = "".join(f", {name}={value}" for name, value in sorted(variant.items()))
            best = _better(best, spec.handler(**params), f"query {query!r}{filters}", params)
    return best


@pytest.fixture(scope="module")
def peaks(registry: ToolRegistry, conn: sqlite3.Connection) -> dict[str, Peak]:
    """Every registered tool's peak PRE-shed frame at its widest legal request.

    One sweep per session: this is the expensive half of arm (c), and every assertion below
    reads it.
    """
    battery = _query_battery(conn)
    found: dict[str, Peak] = {}
    with _cap_lifted():
        for name in registry.names():
            spec = registry.get(name)
            if name in _ROW_CORPUS:
                found[name] = _sweep_row_tool(spec, conn, _ROW_CORPUS[name])
            elif name in _PAGED_TOOLS:
                found[name] = _sweep_paged_tool(spec)
            elif name in _SEARCH_TOOLS:
                found[name] = _sweep_search_tool(spec, battery)
            else:
                found[name] = _better(Peak(), spec.handler(), "no parameters", {})
    return found


# --- the sweep covers the registry, and reads what it claims to read ------------


def test_every_registered_tool_is_swept(registry: ToolRegistry) -> None:
    # "Never a remembered list of tools": a registered tool in no driver group would
    # be reported as covered by the declaration test and driven by nothing here, which is
    # exactly the half-covered state the earlier defects shipped from. Fails both ways.
    driven = set(_ROW_CORPUS) | set(_PAGED_TOOLS) | set(_SEARCH_TOOLS) | set(_STATIC_TOOLS)
    assert set(registry.names()) - driven == set(), "a registered tool is in no driver group"
    assert driven - set(registry.names()) == set(), "a driver group names no registered tool"


def test_the_cap_lift_is_total(registry: ToolRegistry) -> None:
    # Load-bearing: every figure in this module is a PRE-shed frame, and a lift that did not
    # reach the enforcement point would hand back post-shed bytes while every band below
    # still passed. Proven on the one shape known to overrun -- with the lift on it comes
    # back over the cap, with the lift off it does not.
    spec = registry.get("get_stage")
    params = {
        "server": "cn",
        "game_id": "act1football_01",
        **widest_knobs(spec.input_schema, PRESSURE_BY_TOOL["get_stage"]),
    }
    with _cap_lifted():
        assert wire_size(spec.handler(**params)) > MAX_RESPONSE_BYTES
    assert wire_size(spec.handler(**params)) <= MAX_RESPONSE_BYTES


# --- the pinned distribution ----------------------------------------------------


@pytest.mark.parametrize("row", FRAME_PRESSURE, ids=lambda row: row.tool)
def test_the_measured_peak_stays_within_its_declared_band(
    peaks: dict[str, Peak], row: ToolFramePressure
) -> None:
    # The tripwire. Above the ceiling means the corpus grew a fatter window than anyone
    # counted -- re-count, and give the tool a shed plan if the new peak crossed the cap.
    # Below the floor means the declaration is no longer a pin (a guard that cannot
    # fail is not a guard).
    measured = peaks[row.tool].frame
    ceiling = int(row.peak_frame_bytes * CREEP_CEILING)
    floor = int(row.peak_frame_bytes * STALE_FLOOR)
    assert floor <= measured <= ceiling, (
        f"{row.tool} peaks at {measured} B ({measured / MAX_RESPONSE_BYTES:.1%} of cap) at "
        f"{peaks[row.tool].label}; declared {row.peak_frame_bytes} B, band [{floor}, {ceiling}]"
    )


@pytest.mark.parametrize("row", FRAME_PRESSURE, ids=lambda row: row.tool)
def test_the_peak_shape_is_where_the_declaration_says(
    peaks: dict[str, Peak], row: ToolFramePressure
) -> None:
    # A peak that moved to another row while staying inside the band is still news: the
    # declaration's ``peak_at`` is what a reader checks a figure against, and
    # ``get_stage``'s own peak row is the shape arm (b) was built on.
    assert peaks[row.tool].label == row.peak_at


# --- able to exceed means a LIVE plan, and a live plan means answers ---------


def test_the_declared_live_shedder_set_is_exactly_the_over_cap_set(peaks: dict[str, Peak]) -> None:
    # The clause itself, failing both ways. A tool that can exceed and declares no live plan
    # is the next regression; a tool declaring LIVE that cannot exceed is a claim no build backs.
    # The second direction is why the declaration splits: hoisting the per-ref
    # source_id took get_banners to 86.5% of cap while its plan stayed, so LIVE became false
    # and NONE would have been false too -- the plan is DEAD_TODAY, and the probes below are
    # what keep that from being a way to escape this assertion.
    over = {name for name, peak in peaks.items() if peak.frame > MAX_RESPONSE_BYTES}
    declared = {row.tool for row in FRAME_PRESSURE if row.shed is ShedStatus.LIVE}
    assert over == declared


@pytest.mark.parametrize(
    "row", [r for r in FRAME_PRESSURE if r.shed is ShedStatus.LIVE], ids=lambda row: row.tool
)
def test_every_over_cap_tool_answers_instead_of_withholding(
    registry: ToolRegistry, peaks: dict[str, Peak], row: ToolFramePressure
) -> None:
    # The two historical defects verbatim, at the widest legal request rather than at defaults: the
    # response the cap acts on comes back non-empty and under the cap. Checked as behaviour,
    # so it holds whatever mechanism a tool declares its plan through.
    env = registry.get(row.tool).handler(**peaks[row.tool].params)
    assert env.data != {}, row.tool
    assert wire_size(env) <= MAX_RESPONSE_BYTES, row.tool
    assert env.status in {"ok", "partial"}, row.tool


# --- a plan no build reaches, proven reachable rather than claimed


@contextlib.contextmanager
def _cap_at(limit: int) -> Iterator[None]:
    """Drive the cap to a synthetic value the promoted corpus does not produce.

    The "proven by a value the real corpus does not carry" rule, applied to the cap
    instead of to a column: a ``DEAD_TODAY`` plan is unreachable on this build precisely
    because every window fits, so the only honest proof drives the boundary rather than the
    payload. Lowering the cap is the same synthetic move used for the ``map_image``
    step, which fires on no live stage either.
    """
    original = envelopes.MAX_RESPONSE_BYTES
    envelopes.MAX_RESPONSE_BYTES = limit
    try:
        yield
    finally:
        envelopes.MAX_RESPONSE_BYTES = original


@pytest.mark.parametrize(
    "row", [r for r in FRAME_PRESSURE if r.shed is ShedStatus.DEAD_TODAY], ids=lambda row: row.tool
)
def test_a_dead_today_plan_still_fires_under_a_lowered_cap(
    registry: ToolRegistry, peaks: dict[str, Peak], row: ToolFramePressure
) -> None:
    # The declaration's load-bearing half. DEAD_TODAY says "the plan is kept because it is
    # reachable by construction", and the rule makes that a proof rather than a sentence: with
    # the cap under this tool's own peak the plan has to fire and the answer has to survive
    # -- non-empty data, under the lowered cap, with a limitation naming what left.
    # A plan that had been deleted while the declaration stayed fails here, which is exactly
    # the drift a third status could otherwise hide.
    peak = peaks[row.tool]
    with _cap_at(peak.frame - 1):
        env = registry.get(row.tool).handler(**peak.params)
        assert env.data != {}, row.tool
        assert wire_size(env) <= peak.frame - 1, row.tool
        assert env.status in {"ok", "partial"}, row.tool
        assert env.limitations != (), row.tool


@pytest.mark.parametrize(
    "row", [r for r in FRAME_PRESSURE if not r.declares_plan], ids=lambda row: row.tool
)
def test_a_tool_with_no_plan_withholds_under_a_lowered_cap(
    registry: ToolRegistry, peaks: dict[str, Peak], row: ToolFramePressure
) -> None:
    # The other direction, and the reason the pair is one guard: without it, NONE and
    # DEAD_TODAY are indistinguishable by execution and the difference is prose. A tool
    # declaring no plan must take the fail-closed withhold when its frame does not fit
    # -- empty data, `partial`, and never an oversized frame on the wire.
    peak = peaks[row.tool]
    with _cap_at(peak.frame - 1):
        env = registry.get(row.tool).handler(**peak.params)
        assert dict(env.data) == {}, row.tool
        assert env.status == "partial", row.tool
        assert wire_size(env) <= peak.frame - 1, row.tool


@pytest.mark.parametrize(
    "row", [r for r in FRAME_PRESSURE if r.shed is not ShedStatus.LIVE], ids=lambda row: row.tool
)
def test_a_tool_that_fits_is_untouched_by_the_cap(
    registry: ToolRegistry, peaks: dict[str, Peak], row: ToolFramePressure
) -> None:
    # The other direction: nothing sheds a response that fits. Compared against the
    # cap-lifted response rather than against shed-limitation marker strings, so this stays
    # tool-agnostic and keeps holding when a tool declares a plan later -- ``get_stage``'s
    # ``cn/act2multi_tr02`` at 95.5% of the cap is the tightest live example of what must
    # survive whole.
    spec = registry.get(row.tool)
    params = peaks[row.tool].params
    enforced = spec.handler(**params)
    with _cap_lifted():
        lifted = spec.handler(**params)
    stable = {name: value for name, value in enforced.data.items() if name not in row.volatile}
    assert stable == {n: v for n, v in lifted.data.items() if n not in row.volatile}, row.tool
    assert enforced.limitations == lifted.limitations, row.tool
    assert enforced.status == lifted.status, row.tool
    assert enforced.data != {}, row.tool
    # Both ways on the exemption itself: a stale ``volatile`` entry would be a hole in the
    # comparison above, hiding a real degradation behind a field nobody compares.
    for name in row.volatile:
        assert name in enforced.data, (row.tool, name, "volatile field is not published")
        assert enforced.data[name] != lifted.data[name], (row.tool, name, "does not vary")


# --- "every include flag on" is the widest shape, counted ------------


@pytest.mark.parametrize("row", FRAME_PRESSURE, ids=lambda row: row.tool)
def test_all_flags_on_is_the_widest_shape(
    registry: ToolRegistry, peaks: dict[str, Peak], row: ToolFramePressure
) -> None:
    # The widest legal request is every include flag ON, which assumes a flag
    # only ever ADDS. That is an assumption about the tool, so it is counted rather than
    # taken: on this tool's own peak row, every combination of its flags is driven and
    # all-on has to be the maximum. A flag that swapped a section for a smaller one would
    # mean the sweep above had been reading a shape narrower than the request space.
    spec = registry.get(row.tool)
    pressure = PRESSURE_BY_TOOL[row.tool]
    kinds = classify_inputs(spec.input_schema, pressure)
    flags = tuple(name for name, kind in kinds.items() if kind is InputKind.FLAG)
    if not flags:
        pytest.skip(f"{row.tool} publishes no include flags")
    params = peaks[row.tool].params
    with _cap_lifted():
        all_on = wire_size(spec.handler(**params))
        for combo in itertools.product((True, False), repeat=len(flags)):
            variant = {**params, **dict(zip(flags, combo, strict=True))}
            assert wire_size(spec.handler(**variant)) <= all_on, (row.tool, variant)
