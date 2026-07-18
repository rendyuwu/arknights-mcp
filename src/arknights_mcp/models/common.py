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

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: §V5 supported regions. A factual tool requires one; search may filter by one.
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


def tool_input_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema for a tool's ``inputSchema`` (§T30 -> §T29 registry bridge).

    Returns the model's JSON Schema object -- ``type: object`` with the bounded
    field constraints (``maximum``/``minimum``/``maxLength``) and
    ``additionalProperties: false`` (from ``extra="forbid"``) -- so the bounds a
    client sees on the wire are exactly the ones the model enforces (§V18/§V19/§V22).
    """
    return model.model_json_schema()
