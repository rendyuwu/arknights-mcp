"""Shared bases + bounds for the tool input/output models (§T30; §V22).

Every MCP tool declares its parameters as a bounded Pydantic v2 model built on
:class:`StrictModel`. "Bounded" is the point of this task: the numeric/string
:class:`~pydantic.Field` constraints below are the enforcement point for the
data-minimisation invariants, and they surface *on the wire* -- ``model_json_schema``
carries ``maximum``/``minimum``/``maxLength`` + ``additionalProperties: false`` into
the tool ``inputSchema`` a client sees.

Three invariants live here:

* **§V22** -- default responses stay small: heavy map/spawn payloads are opt-in
  via include flags (default off, see the stage models) and pagination is bounded
  by :class:`PageParams` (``page_size <= PAGE_SIZE_MAX``). :class:`PageInfo` is the
  paired bounded output descriptor.
* **§V19** -- no bulk dump / unbounded pagination / enumeration: search results are
  capped (:data:`SEARCH_DEFAULT_LIMIT` .. :data:`SEARCH_MAX_LIMIT`) and ``page_size``
  at :data:`PAGE_SIZE_MAX`. Out-of-range input is *rejected*, not silently widened.
* **§V18** -- client-supplied strings are untrusted: every string field is length
  capped (:data:`MAX_QUERY_LEN` / :data:`MAX_ID_LEN`) and ``extra="forbid"`` rejects
  unknown parameters, so a crafted request cannot smuggle extra data past the model.

Region (§V5): a factual tool takes ``server`` as :data:`Region` (``en``|``cn``)
so a region is always attributed and the two are never silently mixed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

#: §V5 supported regions. A factual tool requires one; search may filter by one.
#: The extra-locale (ja/ko) NAME-alias axis is RETIRED (§V57, T156 -- founder
#: 2026-07-23, EN+CN only): there is no ``locale`` filter and no fact region beyond
#: these two.
Region = Literal["en", "cn"]

#: §V19 search-result window. Single home for these bounds (§V37): the search
#: service imports them rather than re-declaring, so model + service never diverge.
SEARCH_DEFAULT_LIMIT = 10
SEARCH_MAX_LIMIT = 50

#: §V19 pagination bounds for large detail payloads (§V22 opt-in map/spawn/etc.).
PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 100

#: §V18 length caps for untrusted client strings. A query is free text; an id /
#: code is a lookup key -- both are bounded so a request cannot carry a huge blob.
MAX_QUERY_LEN = 200
MAX_ID_LEN = 128


class StrictModel(BaseModel):
    """Base for every tool input/output model (§V18).

    ``extra="forbid"`` rejects unknown parameters (a crafted request cannot smuggle
    fields past validation); ``frozen`` makes a validated model immutable; whitespace
    on string fields is stripped so bounds/caps apply to the trimmed value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class PageParams(StrictModel):
    """Bounded pagination request for a large detail payload (§V19/§V22).

    ``page`` is 1-based; ``page_size`` is capped at :data:`PAGE_SIZE_MAX` so no
    single request can pull an unbounded slice (§V19). Tools that expose a heavy
    section (map tiles, spawns, routes) page it through these bounds (§V22).
    """

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX)


class PageInfo(StrictModel):
    """Bounded pagination descriptor returned alongside a paged payload (§V22).

    Lets a client page deterministically without ever requesting an unbounded
    slice: ``has_more`` signals another bounded page rather than inviting a dump.
    """

    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=PAGE_SIZE_MAX)
    total: int = Field(ge=0)
    has_more: bool


def validate_iso_bound(value: str | None) -> str | None:
    """Reject a since/until date bound that is not an ISO date/datetime (§V19; §V37).

    The single home (§V37) for the date-filter bound validator shared by every list
    tool with a since/until window (``get_announcements`` §T96/B48, ``get_banners``
    §T114). The stored date column is compared lexicographically at the SQL layer (an
    ISO-8601 string sorts in date order), so a non-date bound like ``"july"`` passes the
    length cap yet sorts BEFORE every ISO date, silently emptying the windowed query
    with no error -- a caller cannot tell "nothing in range" from "malformed bound"
    (B48). :func:`datetime.fromisoformat` accepts both a bare ISO date and a full ISO
    datetime (and validates the calendar parts), so a value it rejects surfaces as a
    protocol-level ``ValidationError`` at the model gate rather than a degenerate empty
    result.
    """
    if value is None:
        return value
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        # Client-facing message (it surfaces in the §V71 (c) invalid_input envelope),
        # so no internal cite: keep the behavioral sentence, drop the §V tag.
        raise ValueError(
            f"must be an ISO date (YYYY-MM-DD) or ISO datetime; got {value!r}"
        ) from exc
    return value


class WindowBounds(Protocol):
    """Structural shape of a model carrying a since/until window (§V105; §V37).

    The two windowed tool inputs (``GetAnnouncementsInput``, ``GetBannersInput``) declare
    their own ``since``/``until`` fields, so the shared cross-field validator binds to
    this shape rather than to a base class -- a base class would reorder the published
    ``inputSchema`` properties for no behavioural gain.
    """

    since: str | None
    until: str | None


def inverted_window_reason(since: str | None, until: str | None) -> str | None:
    """Why a since/until pair can match nothing, or ``None`` when it can (§V105).

    The single home (§V37) for the bound-RELATION check, shared by the model gate of
    every windowed tool and by the services behind them. B49/B48 typed the bound SHAPE
    (:func:`validate_iso_bound`); this types the RELATION between the pair, which is the
    other half of the same one-check-not-both defect (B143): ``since=2026-07-01,
    until=2026-06-01`` used to return ``ok`` + an empty list, indistinguishable from a
    genuinely empty window, so a client concluded nothing had been announced.

    The comparison is LEXICOGRAPHIC because that is exactly the comparison the window
    performs: both repositories filter with ``column >= :since`` / ``column <= :until``
    over a TEXT column, so string order *is* the window's order. Strings are totally
    ordered, so ``since > until`` means no stored value can satisfy both bounds at once --
    the emptiness is a property of the input, not an answer about the data. That also
    makes rejecting such a pair safe: it can never withhold a row the query would have
    returned.

    Two different mistakes produce that inversion, and the returned sentence names the
    one that actually happened -- claiming "since is later than until" of a pair that is
    chronologically fine would be a false statement about the caller's input:

    * a genuine inversion -- the caller swapped the bounds;
    * mixed ISO forms -- :func:`datetime.fromisoformat` accepts ISO *basic* format, so
      ``since="20260801"`` passes the shape gate yet sorts above every ``2026-09-01``
      style value (``-`` sorts below a digit), inverting a chronologically correct pair.

    Both bounds have already passed :func:`validate_iso_bound` when this runs (field
    validators precede a model validator), so both parse. An equal pair is a legitimate
    single-day window and is never rejected.
    """
    if since is None or until is None or since <= until:
        return None
    if datetime.fromisoformat(since) <= datetime.fromisoformat(until):
        # Chronologically fine, so the fault is the notation, not the order: say so
        # rather than accusing the caller of swapping bounds they did not swap.
        return (
            f"since {since!r} and until {until!r} are written in different ISO forms, so "
            "the date comparison orders them backwards and the window can match nothing; "
            "write both as YYYY-MM-DD"
        )
    return (
        f"since {since!r} is later than until {until!r}, so the window can match "
        "nothing; swap the two bounds"
    )


def reject_inverted_window(since: str | None, until: str | None) -> None:
    """Raise on a since/until pair whose window is empty by construction (§V105; §V23).

    The enforcement point shared by the model gate (via :func:`validate_window_order`)
    and by the services behind it, mirroring how §V19's page bounds are rejected at BOTH
    the model and the service -- one contract, both places, never a silent empty in
    either. A caller reaching a windowed service directly gets the same
    :class:`ValueError`; through MCP the model gate fires first and the rejection reaches
    the client as the typed §V71 (c) ``invalid_input`` envelope.
    """
    reason = inverted_window_reason(since, until)
    if reason is not None:
        raise ValueError(reason)


def validate_window_order[ModelT: WindowBounds](model: ModelT) -> ModelT:
    """Bind :func:`reject_inverted_window` as an ``after`` model validator (§V105).

    Each windowed tool input binds this, so the rejection surfaces through the standard
    §V71 (c) ``invalid_input`` envelope carrying the reason's own wording (Pydantic's
    ``"Value error, "`` prefix is stripped there). The message names both offending
    values itself, because a model-level validator reports no field ``loc`` for the
    envelope to name.
    """
    reject_inverted_window(model.since, model.until)
    return model


#: JSON Schema keys whose *value* is a map of caller-chosen names -> subschema
#: (property names, ``$defs`` names, ...), NOT a map of schema keywords. A name here
#: may legitimately be ``"description"`` (a model field literally named ``description``)
#: and must survive on the wire, even though its subschema is still descended into. Used
#: by :func:`_strip_schema_descriptions` to key the strip off schema *position* rather
#: than the literal key string.
_SCHEMA_NAME_MAP_KEYS = frozenset(
    {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}
)


def _strip_schema_descriptions(node: Any) -> Any:
    """Recursively drop the schema-level ``description`` keyword from a generated schema.

    §V71 (b): a model's class docstring is published verbatim as the schema's
    ``description`` by :meth:`~pydantic.BaseModel.model_json_schema`, and those
    docstrings carry internal spec cites (``§V…`` / ``B…`` / ``§T…``) + jargon
    ("degenerate", "asymmetric-broken") written for maintainers. Those must not reach
    an MCP client. Rather than rewrite every docstring (and lose the cites the code
    should keep), the single §V37 home strips the auto-generated ``description`` from
    the *published* schema here: the behavioral guidance for the client lives in the
    tool-level ``description`` (kept, cite-free), while the structural contract
    (types, ``maximum``/``minimum``/``maxLength``, ``required``,
    ``additionalProperties: false``) is preserved intact. The cites stay in the code
    docstrings, never on the wire.

    The strip keys off schema *position*, not the literal string: ``description`` is
    dropped only where it is a schema *keyword*. Inside a name-map (``properties`` /
    ``$defs`` / ..., see :data:`_SCHEMA_NAME_MAP_KEYS`) the keys are caller-chosen names,
    so a model field literally named ``description`` survives on the wire (its subschema
    is still descended into) -- preserving the wire↔model parity §V18/§V19 promise that
    the bounds a client sees are exactly the ones the model enforces.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "description":
                # A schema-level ``description`` keyword (the auto-published docstring
                # or a Field ``description``): drop it. A property/def literally named
                # "description" is NOT here -- it is a key inside a name-map, handled
                # in the branch below, so it is preserved.
                continue
            if key in _SCHEMA_NAME_MAP_KEYS and isinstance(value, dict):
                # The value maps caller-chosen names -> subschema: keep every name
                # verbatim (a name may be "description"), but descend into each
                # subschema so a nested keyword ``description`` is still stripped.
                out[key] = {
                    name: _strip_schema_descriptions(subschema) for name, subschema in value.items()
                }
            else:
                out[key] = _strip_schema_descriptions(value)
        return out
    if isinstance(node, list):
        return [_strip_schema_descriptions(item) for item in node]
    return node


def tool_input_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema for a tool's ``inputSchema`` (§T30 -> §T29 registry bridge).

    Returns the model's JSON Schema object -- ``type: object`` with the bounded
    field constraints (``maximum``/``minimum``/``maxLength``) and
    ``additionalProperties: false`` (from ``extra="forbid"``) -- so the bounds a
    client sees on the wire are exactly the ones the model enforces (§V18/§V19/§V22).

    The auto-generated ``description`` (the model/field docstring) is stripped
    (:func:`_strip_schema_descriptions`, §V71 (b)): docstrings carry internal spec
    cites/jargon that must not reach a client, and the client-facing behavioral text
    lives in the tool-level ``description`` instead.
    """
    return cast("dict[str, Any]", _strip_schema_descriptions(model.model_json_schema()))
