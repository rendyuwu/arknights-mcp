"""``get_data_status`` + ``get_data_sources`` MCP tools (§T77; §V5/§V27; §I.tool).

These two data-metadata tools bring the MCP tool surface to the full §I.tool set
of nine: they report *server-side posture* -- the active build's data status and
the public-safe source registry -- rather than an entity lookup. Both bridge an
empty bounded input model (§T30 -- ``extra="forbid"`` still rejects any smuggled
parameter, §V18) to a shared T27 domain service (§V14 -- the ``status``/``doctor``
CLI and the ``arknights://`` resources call the same services, so there is no
second query path) and wrap the outcome in the typed
:class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope` (§V21/§V22/§V23). Neither
owns query logic of its own -- only the model -> service -> envelope mapping -- so
both transports dispatch identical read-only (§V2) behaviour from the one registry.

Two invariants are load-bearing here:

* **§V5** -- ``get_data_status`` tags every active snapshot with its region and
  emits one provenance entry per snapshot (region + snapshot_id + imported_at), so
  a build spanning en + cn is region-attributed and the two are never silently
  mixed.
* **§V27/§V34** -- ``get_data_sources`` routes through
  :func:`~arknights_mcp.services.source_status.get_data_sources` ->
  ``registry.public_view``, the single public-safe projection: it never
  re-enumerates the allowlist, so it cannot leak secrets, local paths, OAuth
  config, or takedown/policy notes.
"""

from __future__ import annotations

from arknights_mcp.mcp.envelopes import Provenance, ResponseEnvelope, build_envelope, ok
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._shared import ConnectionProvider, run_guarded
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.sources import GetDataSourcesInput, GetDataStatusInput
from arknights_mcp.services.source_status import DataSourcesResult, get_data_sources
from arknights_mcp.services.status import DataStatus, get_data_status
from arknights_mcp.sources.registry import SourceRegistry

_STATUS_TOOL_NAME = "get_data_status"
_STATUS_TOOL_TITLE = "Get data status"
_STATUS_TOOL_DESCRIPTION = (
    "Report the active build's data status: schema + analyzer version, deployment "
    "mode, and the active snapshots per region (source, commit/version, import "
    "time, age) with any staleness warnings and a suggested admin action. en/cn "
    "are never mixed."
)

_SOURCES_TOOL_NAME = "get_data_sources"
_SOURCES_TOOL_TITLE = "Get data sources"
_SOURCES_TOOL_DESCRIPTION = (
    "List the public-safe source registry: id, owner, canonical URL, purpose + "
    "consumed fields, region coverage, license/permission posture, attribution, "
    "and the active snapshot per region. No secrets, local paths, or OAuth config."
)


def _status_to_envelope(status: DataStatus) -> ResponseEnvelope:
    """Map the shared :class:`DataStatus` to a typed §V23 envelope (§V5).

    Each active snapshot contributes one region-scoped provenance entry (§V5:
    region + snapshot_id + imported_at travel with the facts); the service's own
    ``ok``/``data_stale`` verdict becomes the envelope status. The full status body
    (schema/analyzer version, mode, per-region snapshots, warnings, action) is the
    service's own serialization -- reused, not re-enumerated (§V37).
    """
    provenance = tuple(
        Provenance(server=s.server, snapshot_id=s.snapshot_id, imported_at=s.imported_at)
        for s in status.snapshots
    )
    return build_envelope(
        status.status,
        data=status.to_dict(),
        provenance=provenance,
        analyzer_version=status.analyzer_version,
    )


def _sources_to_envelope(result: DataSourcesResult) -> ResponseEnvelope:
    """Wrap the public-safe registry projection in an ``ok`` envelope (§V27/§V34)."""
    return ok(result.to_dict())


def build_get_data_status_spec(get_conn: ConnectionProvider, *, mode: str = "local") -> ToolSpec:
    """Build the ``get_data_status`` :class:`ToolSpec` (§T77; §V5/§V14).

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build; ``mode`` is the deployment-mode label reported in the status body. The
    spec is read-only (§V2) for the single shared registry both transports dispatch
    from (§V14); its ``input_schema`` is the empty bounded model, so a smuggled
    parameter is rejected on the wire (§V18) before any query runs.
    """

    def handler(**params: object) -> ResponseEnvelope:
        # §V18 gate: the empty bounded model rejects any unknown parameter before a
        # query runs (a ValidationError propagates as a protocol-level rejection).
        GetDataStatusInput.model_validate(params)
        return run_guarded(
            get_conn,
            lambda conn: get_data_status(conn, mode=mode),
            _status_to_envelope,
        )

    return ToolSpec(
        name=_STATUS_TOOL_NAME,
        title=_STATUS_TOOL_TITLE,
        description=_STATUS_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(GetDataStatusInput),
    )


def build_get_data_sources_spec(
    get_conn: ConnectionProvider, *, registry: SourceRegistry
) -> ToolSpec:
    """Build the ``get_data_sources`` :class:`ToolSpec` (§T77; §V27/§V34/§V14).

    ``registry`` is the authoritative live source posture (enabled/disabled reflects
    the machine registry); the service annotates it with the active snapshot per
    region from the read-only ``get_conn`` build. The projection is the single
    ``registry.public_view`` allowlist (§V34) -- no secrets, local paths, OAuth
    config, or policy notes reach the client (§V27). Read-only (§V2) for the shared
    registry both transports use (§V14).
    """

    def handler(**params: object) -> ResponseEnvelope:
        GetDataSourcesInput.model_validate(params)
        return run_guarded(
            get_conn,
            lambda conn: get_data_sources(registry, conn),
            _sources_to_envelope,
        )

    return ToolSpec(
        name=_SOURCES_TOOL_NAME,
        title=_SOURCES_TOOL_TITLE,
        description=_SOURCES_TOOL_DESCRIPTION,
        handler=handler,
        input_schema=tool_input_schema(GetDataSourcesInput),
    )
