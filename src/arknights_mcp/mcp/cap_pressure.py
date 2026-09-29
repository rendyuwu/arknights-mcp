"""Which tools can overrun the response cap, counted.

The shed rule says what an over-cap response *emits*: shed the knob-bounded part instead of
withholding the whole answer. ``get_banners`` and ``get_stage`` each
declare such a plan now. This module is the clause that says those two fixes are not the
whole job.

Both bugs shipped for the *same* reason, one tool apart: **no test drove a tool at its
widest legal request over the real corpus**. Every cap test used defaults, and a payload
that only overruns at ``page_size=100`` with every include flag on is invisible at
defaults. So arm (a) fixed one shape, arm (b) fixed another, and nothing noticed the third.

Two halves, both here:

* **the widest legal request, derived rather than remembered.** It is every include flag on
  and every knob at its maximum. That definition is executable against a tool's own
  published ``inputSchema``, so a flag or knob added later is picked up without anyone
  remembering to widen a sweep. :func:`classify_inputs` sorts every published property into
  exactly one :class:`InputKind` and *raises* on one it cannot place -- a new kind of
  parameter fails loudly instead of being silently left at its default, which is the failure
  that produced both bugs.
* **the counted distribution.** :data:`FRAME_PRESSURE` records, per registered tool, the
  peak result-frame size at that request over the whole promoted build, and whether the
  tool therefore needs a shed plan. The figures name their basis
  (:data:`CAP_PRESSURE_BASIS`) because a bare number is not re-checkable and a later
  widening of the request space invalidates it silently.

The basis is the *promoted build's* identity rather than the upstream snapshot's: a
``FIELD_POLICY_VERSION``/``TRANSFORM_VERSION`` bump reshapes the payload over byte-identical
upstream input, so a distribution pinned to the snapshot would read valid across exactly the
change most likely to move it.

What this module deliberately does NOT hold is a list of tools. The guard
(``tests/contract/test_frame_pressure.py``) enumerates
:meth:`~arknights_mcp.mcp.tool_registry.ToolRegistry.names` and fails when a registered
tool has no row here and when a row here names no registered tool -- "never a
remembered list of tools", and the same anti-drift shape as
:func:`tests.support.tool_calls.assert_every_tool_is_covered` and
:data:`~arknights_mcp.analyzers.rules.RULE_LIMITATION_ARMS`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

#: The promoted build every figure in :data:`FRAME_PRESSURE` was counted on,
#: except the three account-tool rows, which name their own counting build.
#: The guard re-derives the *classification* from whatever build is promoted, so this is
#: the basis of the numbers, not a gate on running.
CAP_PRESSURE_BASIS = "2026-08-13T220624Z-en-cn"

#: How far above its declared peak a re-measured tool may drift before the tripwire fires.
#: This is the whole point of arm (c): a corpus that grows a fatter window must fail the
#: gate rather than ship an empty answer, and 10% is small enough that a single new event's
#: heaviest stage moves it. Firing is not a false alarm -- it is the signal to re-count, and
#: to add a shed plan if the new peak crossed the cap.
CREEP_CEILING = 1.10

#: How far *below* its declared peak a re-measured tool may sit. A declaration parked well
#: above the real peak is not a pin -- it is a guard that cannot fail, and it would
#: let the ceiling above absorb real growth in silence. Fails the other way, on purpose.
STALE_FLOOR = 0.80


class CapPressureError(ValueError):
    """Raised when a published input property cannot be classified.

    Loud rather than lenient. The lenient version of this -- treat an unrecognised property
    as "leave at its default" -- is exactly how a new knob would keep the sweep narrower
    than the request space a client can actually reach, which is the shape of both shipped
    bugs.
    """


class ShedStatus(Enum):
    """Whether a tool declares a shed plan, and whether a build still exercises it.

    This distinction was forced open. It replaced a boolean that conflated two questions,
    and the conflation only stayed invisible while both answers happened to agree:
    ``get_banners`` declared a plan AND overran the cap, so one flag served. Hoisting the
    per-ref ``source_id`` took its peak to 86.5% of the cap without retiring the plan, and a
    boolean then had no honest value -- ``True`` fails "the shedder set is exactly the
    over-cap set", ``False`` says the code carries no plan when it does.

    So the three states are separated, using the liveness vocabulary (``live`` / ``dead_today``):

    * :attr:`NONE` -- no plan; over the cap this tool takes the fail-closed withhold.
    * :attr:`LIVE` -- a plan, and the promoted build reaches it.
    * :attr:`DEAD_TODAY` -- a plan the promoted build does not reach, kept because it is
      reachable BY CONSTRUCTION. Keeping this a declaration rather than an excuse: a
      ``dead_today`` arm must stay reachable and be PROVEN so synthetically, and an arm that
      is unreachable is retired instead. The alternative -- delete a plan the
      moment a build stops needing it -- trades the shed guarantee for an economy, which is
      exactly the swap the earlier lesson warned against ("economy buys margin, the rule buys
      the guarantee -- ship both").

    Both directions are checked by ``tests/contract/test_frame_pressure.py``: a
    ``dead_today`` plan must still fire under a synthetically lowered cap, and a ``NONE``
    tool must still withhold under one. Without that second half the two states would be
    indistinguishable by execution, and ``dead_today`` would become a way to keep a
    declaration for a plan somebody had already deleted.
    """

    NONE = "none"
    LIVE = "live"
    DEAD_TODAY = "dead_today"


#: The states that mean "this tool declares a plan", so the plan is expected to fire when
#: the frame does not fit. Derived once here rather than spelled at each guard.
PLAN_DECLARED = frozenset({ShedStatus.LIVE, ShedStatus.DEAD_TODAY})


class InputKind(Enum):
    """What a published tool parameter is, for the purpose of widening a request.

    Three of these are supplied or omitted rather than widened: an ``IDENTITY``/``SELECTOR``
    names the row to fetch (the sweep supplies it from the corpus) and a ``WINDOW_FILTER``
    only ever narrows, so its widest value is *absent*. ``ENUM_FILTER`` is the one filter
    that has to be swept rather than omitted: it changes *which* rows come back, not how
    many, and a fatter row class can peak higher than the unfiltered mix (on the promoted
    build ``search_entities`` peaks at ``entity_type=stage``, not at no filter at all).

    The rest are the widening knobs: every flag on, every page at its maximum size,
    every bounded integer at its bound, every enum knob at its widest member, every set
    knob holding its whole domain.
    """

    IDENTITY = "identity"
    SELECTOR = "selector"
    WINDOW_FILTER = "window_filter"
    ENUM_FILTER = "enum_filter"
    FLAG = "flag"
    PAGE = "page"
    BOUNDED_INT = "bounded_int"
    ENUM_KNOB = "enum_knob"
    SET_KNOB = "set_knob"


#: The kinds :func:`widest_knobs` widens. The complement is supplied by the sweep
#: (identity/selector), omitted (window filter), or crossed (enum filter).
WIDENED_KINDS = frozenset(
    {
        InputKind.FLAG,
        InputKind.PAGE,
        InputKind.BOUNDED_INT,
        InputKind.ENUM_KNOB,
        InputKind.SET_KNOB,
    }
)


@dataclass(frozen=True, slots=True)
class EnumKnob:
    """A string-enum knob whose widest member cannot be read off the schema.

    ``depth=detailed`` returns more than ``summary`` and ``mode=with_observations`` returns
    more than ``facts_only``, but nothing in JSON Schema says so -- the order is semantic.
    So the widest member is declared, and ``domain`` pins what the schema published when it
    was declared: a new member appearing without a decision about whether it is wider fails
    the guard rather than quietly leaving the sweep one member short of the request space.
    """

    name: str
    widest: str
    domain: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolFramePressure:
    """One registered tool's counted cap pressure.

    ``selectors`` and ``filters`` between them account for every nullable-string property
    the tool publishes: a selector names the row (the sweep supplies one), a filter only
    narrows (widest is absent). They are declared rather than guessed because the two are
    indistinguishable in the schema -- ``get_stage``'s ``game_id`` and ``get_banners``'s
    ``query`` are both "optional string" -- and getting it wrong in the lenient direction
    would sweep a tool at a narrowed window while reporting full coverage.

    ``shed`` is the shed claim, in three states rather than two (:class:`ShedStatus`):
    whether this tool declares a plan, and whether the promoted build still reaches it. The
    guard checks it against the measured peak in both directions, and checks the BEHAVIOUR
    (a non-empty answer under the cap; a ``dead_today`` plan firing under a lowered cap;
    a plan-less tool withholding under one) rather than the presence of a ``shed_plan=``
    argument, so the declaration cannot drift from what the tool actually does.

    ``peak_frame_bytes``/``peak_at``/``counted`` are the pinned distribution. The unit is
    the whole ``tools/call`` result frame (:func:`~arknights_mcp.mcp.envelopes.wire_size`:
    both wire copies, the content scaffolding and the JSON-RPC allowance) measured
    **before** any shed, which is the number that decides whether a shed is needed at all.

    ``volatile`` names the top-level ``data`` keys whose value is wall-clock dependent, so
    the guard can prove a fitting response is *unchanged* by the cap without two calls
    having to be byte-identical. Exactly one tool has one (``get_data_status``'s
    ``generated_at``), and the guard fails both ways on it: a stale entry here would mask a
    real degradation, so every declared key must be published AND must actually vary.
    """

    tool: str
    selectors: tuple[str, ...]
    filters: tuple[str, ...]
    enum_knobs: tuple[EnumKnob, ...]
    shed: ShedStatus
    peak_frame_bytes: int
    peak_at: str
    counted: str
    volatile: tuple[str, ...] = ()

    @property
    def declares_plan(self) -> bool:
        """True when this tool carries a shed plan, live or ``dead_today``."""
        return self.shed in PLAN_DECLARED


#: Every registered tool's peak result frame at its widest legal
#: request over the whole promoted build :data:`CAP_PRESSURE_BASIS`, in registration order.
#: Pinned by ``tests/contract/test_frame_pressure.py``, which re-measures on whatever build
#: is promoted and fails when a peak leaves its band, when ``shed`` disagrees with the
#: measurement in either direction, or when a tool is registered without a row here.
#:
#: Three of these figures reproduce earlier counts verbatim -- ``get_stage`` 302656,
#: ``analyze_stage`` 85128, ``get_announcements`` 11013 -- which is what made the sweep
#: trustworthy enough to pin the other thirteen.
FRAME_PRESSURE: tuple[ToolFramePressure, ...] = (
    ToolFramePressure(
        tool="search_entities",
        selectors=(),
        filters=(),
        enum_knobs=(),
        shed=ShedStatus.NONE,
        peak_frame_bytes=30285,
        peak_at="query 'g', entity_type=stage, server=cn",
        counted=(
            "15.1% of cap. Swept the query battery x every entity_type x every server, "
            "limit=50, pre-shed frame bytes. Bounded by construction at 50 locator rows of "
            "capped fields, and the peak is a filtered class rather than the "
            "unfiltered mix -- stage locators are the fattest row kind."
        ),
    ),
    ToolFramePressure(
        tool="search_stages",
        selectors=(),
        filters=(),
        enum_knobs=(),
        shed=ShedStatus.NONE,
        peak_frame_bytes=30285,
        peak_at="query 'g', server=cn",
        counted=(
            "15.1% of cap. Same battery x every server, limit=50, pre-shed frame bytes. "
            "Same 50-row bound, and the same rows search_entities peaks on: this tool "
            "returns stages only, so its peak IS that class."
        ),
    ),
    ToolFramePressure(
        tool="get_stage",
        selectors=("stage_code", "game_id"),
        filters=(),
        enum_knobs=(),
        shed=ShedStatus.LIVE,
        peak_frame_bytes=302656,
        peak_at="cn/act1football_01",
        counted=(
            "151.3% of cap -- the widest legal shape the sweep counted, and the reason arm (b) "
            "exists. 6716 stages x all four include flags x routes_page/spawns_page "
            "page_size=100, pre-shed frame bytes. Route-heavy (routes 67.6% of the "
            "payload, map_image 19.7%, spawns 11.3%), so the plan orders by the measured "
            "weight of the response in hand."
        ),
    ),
    ToolFramePressure(
        tool="get_enemy",
        selectors=(),
        filters=(),
        enum_knobs=(),
        shed=ShedStatus.NONE,
        peak_frame_bytes=7947,
        peak_at="en/enemy_1550_dhnzzh",
        counted=(
            "4.0% of cap. 3879 enemies, pre-shed frame bytes; the tool publishes no flag "
            "and no page, so one shape per enemy IS its widest legal request. This is the "
            "one surface the hoist COSTS: a response carries exactly one ref, so the "
            "response-level image_refs_source_id is wider than the per-ref copy it replaced "
            "and the peak grew 22 bytes from 7925. Declared, not discovered -- the hoist has "
            "to be uniform or one key means two things."
        ),
    ),
    ToolFramePressure(
        tool="get_operator",
        selectors=(),
        filters=(),
        enum_knobs=(),
        shed=ShedStatus.NONE,
        peak_frame_bytes=97839,
        peak_at="cn/char_1033_swire2",
        counted=(
            "48.9% of cap -- the widest headroom of any tool that declares no plan. 884 "
            "operators x all six include flags, pre-shed frame bytes. This row is where "
            "the scope claim was tested and failed: it called the per-ref source_id the "
            "biggest lever for every refs-bearing surface, and counted, this surface carries "
            "6167 refs over 884 rows -- about 7 a row, not the fat gallery the claim assumed. "
            "The hoist moved this peak 470 bytes, 0.24% of cap, from 98309. It rides "
            "along for shape uniformity, not for bytes."
        ),
    ),
    ToolFramePressure(
        tool="compare_operator_modules",
        selectors=(),
        filters=(),
        enum_knobs=(
            EnumKnob(
                name="mode", widest="with_observations", domain=("facts_only", "with_observations")
            ),
        ),
        shed=ShedStatus.NONE,
        peak_frame_bytes=40900,
        peak_at="cn/char_1033_swire2",
        counted=(
            "20.4% of cap. 884 operators x levels=[1,2,3] (the whole published domain) x "
            "mode=with_observations, pre-shed frame bytes. Peaks on the same operator "
            "get_operator does, one fifth the frame."
        ),
    ),
    ToolFramePressure(
        tool="analyze_stage",
        selectors=("stage_code", "game_id"),
        filters=(),
        enum_knobs=(
            EnumKnob(name="depth", widest="detailed", domain=("summary", "standard", "detailed")),
        ),
        shed=ShedStatus.NONE,
        peak_frame_bytes=85128,
        peak_at="cn/act1football_s02",
        counted=(
            "42.6% of cap, reproducing the earlier sweep of this surface to the byte -- the "
            "count that said the stage family carries no third live hole. 6716 stages x "
            "depth=detailed, pre-shed frame bytes."
        ),
    ),
    ToolFramePressure(
        tool="get_stage_drops",
        selectors=("stage_code", "game_id"),
        filters=(),
        enum_knobs=(),
        shed=ShedStatus.NONE,
        peak_frame_bytes=14321,
        peak_at="cn/main_06-11",
        counted=(
            "7.2% of cap. 6716 stages x include_efficiency, pre-shed frame bytes. Bounded "
            "by the drop table's own width: one stage drops a handful of items."
        ),
    ),
    ToolFramePressure(
        tool="get_item_drops",
        selectors=(),
        filters=(),
        enum_knobs=(),
        shed=ShedStatus.NONE,
        peak_frame_bytes=41737,
        peak_at="en/randomMaterial_8",
        counted=(
            "20.9% of cap. 310 items x include_efficiency x stages_page/efficiency_page "
            "page_size=100, pre-shed frame bytes. Two pages at their maximum on one item "
            "and still a fifth of the cap."
        ),
    ),
    ToolFramePressure(
        tool="get_announcements",
        selectors=(),
        filters=("since", "until"),
        enum_knobs=(),
        shed=ShedStatus.NONE,
        peak_frame_bytes=11013,
        peak_at="cn page 1, page_size=100",
        counted=(
            "5.5% of cap, reproducing the earlier figure for this tool exactly -- the "
            "count behind 'the other paginated list tool declares no plan'. Every page of "
            "both regions at page_size=100, no date bound, pre-shed frame bytes. Keeping "
            "that claim true is this row's job."
        ),
    ),
    ToolFramePressure(
        tool="get_banners",
        selectors=(),
        filters=("since", "until", "query"),
        enum_knobs=(),
        shed=ShedStatus.DEAD_TODAY,
        peak_frame_bytes=173003,
        peak_at="en page 1, page_size=100",
        counted=(
            "86.5% of cap. Every page of both regions at page_size=100, no date or text "
            "filter, pre-shed frame bytes; all nine reachable windows land in [9.3%, 86.5%]. "
            "This shape is the original defect's -- it read 220131 (110.1%) until the hoist "
            "moved the per-ref source_id, and 21.4% of the frame was that one repeated "
            "constant. So the plan "
            "arm (a) built is DEAD_TODAY rather than retired: nothing live reaches it, but "
            "page_size tops out at 100 while a banner's featured_ops count is the source's "
            "to decide, so a fat event re-crosses 13.5 points of margin. Its order still "
            "holds where it matters -- the ref step takes this window to 68444 with all 100 "
            "rows aboard, and trimming rows to that size keeps 6."
        ),
    ),
    ToolFramePressure(
        tool="get_my_roster",
        selectors=(),
        filters=("min_rarity", "min_elite"),
        enum_knobs=(),
        shed=ShedStatus.NONE,
        peak_frame_bytes=2874,
        peak_at="en page 1, page_size=100",
        counted=(
            "1.4% of cap, counted on build 2026-09-27T220724Z-en-cn.sqlite with the synthetic "
            "account fixture tests/fixtures/account/sync_data_en.json, pre-shed frame bytes. "
            "A real roster is personal and never in the basis; a full 100-row page of real "
            "operator ids is proven under the cap by the account roster cap contract test."
        ),
    ),
    ToolFramePressure(
        tool="get_my_operator",
        selectors=(),
        filters=(),
        enum_knobs=(),
        shed=ShedStatus.NONE,
        peak_frame_bytes=3657,
        peak_at="en/char_002_amiya",
        counted=(
            "1.8% of cap, counted on build 2026-09-27T220724Z-en-cn.sqlite with the synthetic "
            "account fixture tests/fixtures/account/sync_data_en.json, pre-shed frame bytes. "
            "A real roster is personal and never in the basis; a full 100-row page of real "
            "operator ids is proven under the cap by the account roster cap contract test."
        ),
    ),
    ToolFramePressure(
        tool="get_my_inventory",
        selectors=(),
        filters=(),
        enum_knobs=(),
        shed=ShedStatus.NONE,
        peak_frame_bytes=2190,
        peak_at="en page 1, page_size=100",
        counted=(
            "1.1% of cap, counted on build 2026-09-27T220724Z-en-cn.sqlite with the synthetic "
            "account fixture tests/fixtures/account/sync_data_en.json, pre-shed frame bytes. "
            "A real roster is personal and never in the basis; a full 100-row page of real "
            "operator ids is proven under the cap by the account roster cap contract test."
        ),
    ),
    ToolFramePressure(
        tool="get_data_status",
        selectors=(),
        filters=(),
        enum_knobs=(),
        shed=ShedStatus.NONE,
        peak_frame_bytes=3823,
        peak_at="no parameters",
        counted=(
            "1.9% of cap. The tool publishes no parameters, so its one shape is its widest "
            "legal request; pre-shed frame bytes. Sized by the promoted build's snapshot "
            "manifest, not by the corpus."
        ),
        volatile=("generated_at",),
    ),
    ToolFramePressure(
        tool="get_data_sources",
        selectors=(),
        filters=(),
        enum_knobs=(),
        shed=ShedStatus.NONE,
        peak_frame_bytes=17601,
        peak_at="no parameters",
        counted=(
            "8.8% of cap. One shape, pre-shed frame bytes. Sized by the registered source "
            "count in config/data_sources.toml rather than by any build, so this row moves "
            "when a source is added, never when the corpus grows."
        ),
    ),
)

#: tool name -> its declaration, for the guard and for :func:`widest_knobs`.
PRESSURE_BY_TOOL: Mapping[str, ToolFramePressure] = {row.tool: row for row in FRAME_PRESSURE}


def _resolve(schema: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any]:
    """Follow a local ``$ref`` into the schema's own ``$defs`` (``PageParams``)."""
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    name = ref.rsplit("/", 1)[-1]
    defs = root.get("$defs")
    if not isinstance(defs, Mapping) or not isinstance(defs.get(name), Mapping):
        raise CapPressureError(f"input schema references {ref!r} but publishes no such $defs entry")
    resolved: Mapping[str, Any] = defs[name]
    return resolved


def _nullable_inner(schema: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The non-null branch of an optional property, or ``None`` when it is not optional.

    Pydantic renders ``str | None = None`` as ``anyOf: [{...}, {type: null}]``, which is
    how every filter and every row selector on this surface is published.
    """
    variants = schema.get("anyOf")
    if not isinstance(variants, Sequence):
        return None
    concrete = [v for v in variants if isinstance(v, Mapping) and v.get("type") != "null"]
    nulls = [v for v in variants if isinstance(v, Mapping) and v.get("type") == "null"]
    if len(concrete) != 1 or not nulls:
        return None
    return concrete[0]


def _is_page(schema: Mapping[str, Any]) -> bool:
    props = schema.get("properties")
    return isinstance(props, Mapping) and "page" in props and "page_size" in props


def classify_inputs(
    input_schema: Mapping[str, Any], pressure: ToolFramePressure
) -> dict[str, InputKind]:
    """Sort every published property of one tool into exactly one :class:`InputKind`.

    Reading the schema rather than a hand-written parameter list is the point: this is what
    makes "every include flag on, every knob at its maximum" a construction over the tool's
    real request space instead of a memory of it. A property this cannot place raises
    :class:`CapPressureError`, so the next parameter shape added to the surface stops
    the guard instead of being swept at its default.

    The nullable-string case is the one that cannot be decided from the schema -- a row
    selector and a narrowing filter look identical -- so it is decided by the declaration,
    and a nullable string in neither list raises for the same reason.
    """
    properties = input_schema.get("properties")
    if not isinstance(properties, Mapping):
        return {}
    required = set(input_schema.get("required") or ())
    kinds: dict[str, InputKind] = {}
    for name, raw in properties.items():
        if not isinstance(raw, Mapping):
            raise CapPressureError(f"property {name!r} publishes no schema object")
        schema = _resolve(raw, input_schema)
        if _is_page(schema):
            kinds[name] = InputKind.PAGE
        elif schema.get("type") == "boolean":
            kinds[name] = InputKind.FLAG
        elif schema.get("type") == "integer" and "maximum" in schema:
            kinds[name] = InputKind.BOUNDED_INT
        elif schema.get("type") == "array":
            items = schema.get("items")
            if not isinstance(items, Mapping) or not isinstance(items.get("enum"), Sequence):
                raise CapPressureError(
                    f"property {name!r} is an array with no enumerated item domain, so its "
                    "widest legal value cannot be derived"
                )
            kinds[name] = InputKind.SET_KNOB
        elif isinstance(schema.get("enum"), Sequence) and "default" in raw:
            kinds[name] = InputKind.ENUM_KNOB
        elif (inner := _nullable_inner(schema)) is not None:
            if isinstance(inner.get("enum"), Sequence):
                kinds[name] = InputKind.ENUM_FILTER
            elif name in pressure.selectors:
                kinds[name] = InputKind.SELECTOR
            elif name in pressure.filters:
                kinds[name] = InputKind.WINDOW_FILTER
            else:
                raise CapPressureError(
                    f"{pressure.tool!r} publishes optional property {name!r} and declares it "
                    "neither a row selector nor a narrowing filter, so the sweep cannot know "
                    "whether omitting it narrows the request"
                )
        elif name in required and "default" not in raw:
            kinds[name] = InputKind.IDENTITY
        else:
            raise CapPressureError(
                f"{pressure.tool!r} property {name!r} does not classify: {dict(schema)!r}. A "
                "parameter whose widest legal value is unknown must not be swept at its "
                "default -- that is how both bugs shipped"
            )
    return kinds


def widest_knobs(input_schema: Mapping[str, Any], pressure: ToolFramePressure) -> dict[str, Any]:
    """The knob half of this tool's widest legal request.

    Every flag on, every page at its published maximum size, every bounded integer at its
    bound, every enum knob at its declared widest member, every set knob holding its whole
    domain. Identity/selector arguments are the sweep's to supply and enum filters are the
    sweep's to cross, so neither appears here; a narrowing filter is absent, which is what
    its widest value *is*.
    """
    kinds = classify_inputs(input_schema, pressure)
    properties: Mapping[str, Any] = input_schema.get("properties") or {}
    widest_enum = {knob.name: knob.widest for knob in pressure.enum_knobs}
    args: dict[str, Any] = {}
    for name, kind in kinds.items():
        if kind not in WIDENED_KINDS:
            continue
        schema = _resolve(properties[name], input_schema)
        if kind is InputKind.FLAG:
            args[name] = True
        elif kind is InputKind.PAGE:
            page_size: Mapping[str, Any] = schema["properties"]["page_size"]
            args[name] = {"page": 1, "page_size": page_size["maximum"]}
        elif kind is InputKind.BOUNDED_INT:
            args[name] = schema["maximum"]
        elif kind is InputKind.SET_KNOB:
            args[name] = list(schema["items"]["enum"])
        else:
            if name not in widest_enum:
                raise CapPressureError(
                    f"{pressure.tool!r} publishes enum knob {name!r} and declares no widest "
                    "member; the order of enum members is semantic, not schematic"
                )
            args[name] = widest_enum[name]
    return args


def enum_filter_variants(
    input_schema: Mapping[str, Any], pressure: ToolFramePressure
) -> tuple[dict[str, Any], ...]:
    """Every enum-filter combination the sweep must cross, absent included.

    An enum filter is the one narrowing parameter whose widest value is not "omitted": it
    changes which rows come back rather than how many, so a fatter row class can peak above
    the unfiltered mix. ``search_entities`` does exactly that on the promoted build -- its
    peak frame is ``entity_type=stage``, not no filter at all.
    """
    kinds = classify_inputs(input_schema, pressure)
    properties: Mapping[str, Any] = input_schema.get("properties") or {}
    variants: tuple[dict[str, Any], ...] = ({},)
    for name, kind in kinds.items():
        if kind is not InputKind.ENUM_FILTER:
            continue
        inner = _nullable_inner(_resolve(properties[name], input_schema))
        assert inner is not None  # classify_inputs only assigns ENUM_FILTER to these
        domain = [None, *inner["enum"]]
        variants = tuple(
            {**base, **({} if member is None else {name: member})}
            for base in variants
            for member in domain
        )
    return variants
