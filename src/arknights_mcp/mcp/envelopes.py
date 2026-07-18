"""Typed MCP response envelope (§T29; §I; §V21/§V22/§V23).

Every MCP tool result is wrapped in the same envelope so both transports emit an
identical shape (§V14). The envelope carries, in order (§I):

``schema_version`` -> ``status`` -> ``data`` (facts) -> ``provenance`` ->
``limitations`` -> ``analyzer_version``.

Three invariants live here:

* **§V21** -- every envelope stamps a stable :data:`SCHEMA_VERSION`. Required
  fields stay backward-compatible within v0.1; a breaking change bumps the
  constant and needs an ADR.
* **§V22** -- a default tool response is capped at :data:`MAX_RESPONSE_BYTES`.
  The builder measures the serialized envelope and, when a payload would exceed
  the cap, fails closed to a bounded ``partial`` envelope (data dropped, a cap
  limitation added) rather than emitting an oversized response.
* **§V23** -- every result carries a typed status from :data:`STATUS_VALUES`; an
  unknown status is rejected. Error envelopes never leak a stack trace or local
  path -- :func:`internal_error` emits a fixed, safe message and keeps any
  internal detail out of the response.

Envelopes are plain frozen dataclasses (matching the service layer) with a
``to_dict`` that yields JSON-serializable primitives only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal, get_args

#: §V21 wire-contract version stamped on every envelope. Bump only on a breaking
#: change to a required field, and only alongside an ADR (mirrors ``TRANSFORM``/
#: ``ANALYZER`` versions). Additive optional fields do not bump it.
SCHEMA_VERSION = "0.1"

#: §V22 default response cap. The serialized envelope (as emitted by
#: :meth:`ResponseEnvelope.to_dict` -> JSON) must stay under this size; large
#: map/spawn payloads are opt-in via tool include flags + pagination (§T34).
MAX_RESPONSE_BYTES = 200_000

#: §V23 typed status vocabulary. Every tool result reports exactly one of these.
ToolStatus = Literal[
    "ok",
    "partial",
    "not_found",
    "ambiguous",
    "unsupported_server",
    "data_stale",
    "database_unavailable",
    "schema_incompatible",
    "analysis_unavailable",
    "internal_error",
]

#: The status vocabulary as a runtime set (single source of truth = the Literal).
STATUS_VALUES: frozenset[str] = frozenset(get_args(ToolStatus))

#: Statuses that describe a failure/degradation rather than a delivered result;
#: :func:`error` accepts only these (``ok``/``partial`` go through :func:`ok`).
_ERROR_STATUSES: frozenset[str] = frozenset(
    {
        "not_found",
        "ambiguous",
        "unsupported_server",
        "data_stale",
        "database_unavailable",
        "schema_incompatible",
        "analysis_unavailable",
        "internal_error",
    }
)

#: Fixed, path/trace-free message for an internal failure (§V23).
_INTERNAL_ERROR_MESSAGE = "an internal error occurred while handling the request"


class EnvelopeError(ValueError):
    """Raised when an envelope is constructed with an invalid status (§V23)."""


@dataclass(frozen=True)
class Provenance:
    """Region-scoped provenance for a factual response (§V5).

    Every fact-bearing envelope carries at least one of these so a client can
    attribute the data to a region + snapshot + import time. ``server`` keeps
    en/cn from being silently mixed (§V5).
    """

    server: str
    snapshot_id: str
    imported_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "server": self.server,
            "snapshot_id": self.snapshot_id,
            "imported_at": self.imported_at,
        }


@dataclass(frozen=True)
class ResponseEnvelope:
    """The typed wrapper around every MCP tool result (§I; §V21/§V23).

    Prefer the :func:`ok` / :func:`error` / :func:`build_envelope` builders over
    constructing this directly -- they validate the status (§V23) and enforce the
    §V22 size cap. ``data`` holds the tool-specific facts (already allowlisted +
    sanitized upstream); ``provenance`` is the region/snapshot attribution;
    ``limitations`` records analyzer caveats + any bounding applied.
    """

    status: ToolStatus
    data: Mapping[str, object] = field(default_factory=dict)
    provenance: tuple[Provenance, ...] = ()
    limitations: tuple[str, ...] = ()
    analyzer_version: str | None = None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        # §I field order: schema_version -> status -> data -> provenance ->
        # limitations -> analyzer_version. Dicts preserve insertion order, so the
        # emitted JSON matches the contract shape.
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "data": dict(self.data),
            "provenance": [p.to_dict() for p in self.provenance],
            "limitations": list(self.limitations),
            "analyzer_version": self.analyzer_version,
        }


def serialized_size(envelope: ResponseEnvelope) -> int:
    """Byte size of ``envelope`` as it goes on the wire (compact UTF-8 JSON)."""
    return len(json.dumps(envelope.to_dict(), ensure_ascii=False).encode("utf-8"))


def _validate_status(status: str) -> None:
    if status not in STATUS_VALUES:
        allowed = sorted(STATUS_VALUES)
        raise EnvelopeError(f"unknown tool status {status!r}; must be one of {allowed}")


def _cap_limitation() -> str:
    kib = MAX_RESPONSE_BYTES // 1000
    return (
        f"response exceeded the {kib} KB cap and was withheld; "
        "narrow the query or request less detail (include flags / pagination)"
    )


def _enforce_cap(envelope: ResponseEnvelope) -> ResponseEnvelope:
    """Fail closed to a bounded ``partial`` envelope when over the §V22 cap.

    Rather than emit an oversized response, drop the data payload (provenance +
    limitations stay -- they are small + carry the region attribution) and add a
    cap limitation so the client knows to narrow the request.
    """
    if serialized_size(envelope) <= MAX_RESPONSE_BYTES:
        return envelope
    return ResponseEnvelope(
        status="partial",
        data={},
        provenance=envelope.provenance,
        limitations=(*envelope.limitations, _cap_limitation()),
        analyzer_version=envelope.analyzer_version,
        schema_version=envelope.schema_version,
    )


def build_envelope(
    status: ToolStatus,
    *,
    data: Mapping[str, object] | None = None,
    provenance: Iterable[Provenance] = (),
    limitations: Iterable[str] = (),
    analyzer_version: str | None = None,
) -> ResponseEnvelope:
    """Build a validated, size-bounded envelope (§V21/§V22/§V23).

    Rejects an unknown ``status`` (§V23) and enforces the §V22 cap: a payload
    that would serialize over :data:`MAX_RESPONSE_BYTES` is returned as a bounded
    ``partial`` envelope instead.
    """
    _validate_status(status)
    envelope = ResponseEnvelope(
        status=status,
        data=dict(data) if data is not None else {},
        provenance=tuple(provenance),
        limitations=tuple(limitations),
        analyzer_version=analyzer_version,
    )
    return _enforce_cap(envelope)


def ok(
    data: Mapping[str, object],
    *,
    provenance: Iterable[Provenance] = (),
    limitations: Iterable[str] = (),
    analyzer_version: str | None = None,
) -> ResponseEnvelope:
    """A successful (``ok``) result envelope (§V23). Size-bounded (§V22)."""
    return build_envelope(
        "ok",
        data=data,
        provenance=provenance,
        limitations=limitations,
        analyzer_version=analyzer_version,
    )


def error(
    status: ToolStatus,
    message: str,
    *,
    provenance: Iterable[Provenance] = (),
    limitations: Iterable[str] = (),
    suggested_action: str | None = None,
) -> ResponseEnvelope:
    """A typed error/degraded-result envelope (§V23).

    ``status`` must be a non-``ok``/``partial`` status (e.g. ``not_found``,
    ``database_unavailable``). ``message`` must be a safe, human-readable string
    -- never a stack trace or local path (§V23); callers own that contract, and
    :func:`internal_error` enforces it for the internal-failure path.
    """
    if status not in _ERROR_STATUSES:
        raise EnvelopeError(f"error() requires a failure status; got {status!r}")
    body: dict[str, object] = {"message": message}
    if suggested_action is not None:
        body["suggested_action"] = suggested_action
    return build_envelope(
        status,
        data=body,
        provenance=provenance,
        limitations=limitations,
    )


def internal_error() -> ResponseEnvelope:
    """An ``internal_error`` envelope with a fixed, safe message (§V23).

    The response body carries no exception text, stack trace, or local path --
    those belong in the (redacted) server log, never the client-facing envelope.
    """
    return error("internal_error", _INTERNAL_ERROR_MESSAGE)
