"""``get_data_status`` + ``get_data_sources`` MCP tools.

These two data-metadata tools round out the MCP tool surface: they report
*server-side posture* -- the active build's data status and the public-safe source
registry -- rather than an entity lookup. Both bridge an empty bounded input model
(``extra="forbid"`` still rejects any smuggled parameter) to a shared domain
service (the ``status``/``doctor`` CLI and the ``arknights://`` resources call the
same services, so there is no second query path) and wrap the outcome in the typed
:class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope`. Neither owns query logic of
its own -- only the model -> service -> envelope mapping -- so both transports
dispatch identical read-only behaviour from the one registry.

Two rules are load-bearing here:

* ``get_data_status`` tags every active snapshot with its region and emits one
  provenance entry per snapshot (region + snapshot_id + imported_at), so a build
  spanning en + cn is region-attributed and the two are never silently mixed.
* ``get_data_sources`` routes through
  :func:`~arknights_mcp.services.source_status.get_data_sources` ->
  ``registry.public_view``, the single public-safe projection: it never
  re-enumerates the allowlist, so it cannot leak secrets, local paths, OAuth
  config, or takedown/policy notes.
"""

from __future__ import annotations

from arknights_mcp.mcp.envelopes import Provenance, ResponseEnvelope, build_envelope, ok
from arknights_mcp.mcp.tool_registry import ToolSpec
from arknights_mcp.mcp.tools._shared import (
    ConnectionProvider,
    run_guarded,
    run_registry_guarded,
)
from arknights_mcp.models.common import tool_input_schema
from arknights_mcp.models.sources import GetDataSourcesInput, GetDataStatusInput
from arknights_mcp.services.source_status import DataSourcesResult, get_data_sources
from arknights_mcp.services.status import DataStatus, get_data_status
from arknights_mcp.sources.registry import SourceRegistry

_STATUS_TOOL_NAME = "get_data_status"
_STATUS_TOOL_TITLE = "Get data status"
_STATUS_TOOL_DESCRIPTION = (
    "Report the active build's data status: schema + analyzer version, deployment "
    "mode, and the active snapshots per region (source, commit/version, and age in "
    "days so the client can judge freshness). mode is local (this server runs over "
    "stdio for one local client) or remote (it serves authenticated HTTP); it "
    "describes the deployment, not the data. Each snapshot row carries its server "
    "and snapshot_id inline; the import time travels with the matching response "
    "provenance entry (join on snapshot_id). commit_sha/upstream_version/age_days "
    "appear only when known. Warns when the active build has no snapshots or no "
    "imported entities, with a suggested admin action. en/cn are never mixed."
)

_SOURCES_TOOL_NAME = "get_data_sources"
_SOURCES_TOOL_TITLE = "Get data sources"
_SOURCES_TOOL_DESCRIPTION = (
    "List the public-safe source registry: id, owner, canonical URL, purpose + "
    "consumed fields, region coverage, license/permission posture, attribution, "
    "and the active snapshot per region. No secrets, local paths, or OAuth config. "
    "The four *_status fields (license, permission, private_hosting, "
    "redistribution) are short machine tokens recording this project's own posture "
    "toward the source; they are not a legal opinion, and the set of tokens is "
    "defined by this registry rather than by any standard. internal_ref is a "
    "maintainer bookkeeping note (decision and document ids); ignore it."
)


def _status_to_envelope(status: DataStatus) -> ResponseEnvelope:
    """Map the shared :class:`DataStatus` to a typed envelope.

    Each active snapshot contributes one region-scoped provenance entry (region +
    snapshot_id + imported_at travel with the facts); the service's own
    ``ok``/``data_stale`` verdict becomes the envelope status. The full status body
    (schema/analyzer version, mode, per-region snapshots, warnings, action) is the
    service's own serialization -- reused, not re-enumerated.

    ``get_data_status`` is a *posture* tool: a non-``ok`` result (``data_stale`` on
    an empty/unpromoted build) is a reported state, not a failed request, so it
    keeps the full status body (``warnings`` + ``suggested_action`` name the admin
    action) rather than the ``{message}`` error-body shape :func:`error` emits.
    A client reads the degraded posture from the same ``data`` keys as the ``ok``
    case -- there is no ``message`` key on this tool by design (finding #6).
    """
    provenance = tuple(
        Provenance(server=s.server, snapshot_id=s.snapshot_id, imported_at=s.imported_at)
        for s in status.snapshots
    )
    # ``to_envelope_data`` is the projection for an enveloped caller -- the DB
    # migration id keyed ``db_schema_version`` (``schema_version`` is the envelope's
    # own response-contract version, a different axis entirely), and the
    # ``status``/``analyzer_version`` echoes dropped because the envelope already
    # carries both. The envelope ``provenance`` above is likewise the sole carrier of
    # ``imported_at`` -- trimmed from the ``data.snapshots`` rows so it is emitted
    # once, not duplicated per snapshot. The rows DO inline ``server`` AND
    # ``snapshot_id``: one region can hold several active snapshots (game data +
    # penguin + announcements), so only ``snapshot_id`` joins a row to its provenance
    # entry without the "row N ↔ provenance N" order contract that forbids it. Null
    # commit/version/age keys are omitted by the extras view.
    data = status.to_envelope_data()
    data["snapshots"] = [s.to_provenance_extras(include_server=True) for s in status.snapshots]
    return build_envelope(
        status.status,
        data=data,
        provenance=provenance,
        analyzer_version=status.analyzer_version,
    )


def _sources_to_envelope(result: DataSourcesResult) -> ResponseEnvelope:
    """Wrap the public-safe registry projection in an ``ok`` envelope."""
    return ok(result.to_dict())


def build_get_data_status_spec(get_conn: ConnectionProvider, *, mode: str) -> ToolSpec:
    """Build the ``get_data_status`` :class:`ToolSpec`.

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build; ``mode`` is the deployment-mode label reported in the status body. The
    spec is read-only for the single shared registry both transports dispatch
    from; its ``input_schema`` is the empty bounded model, so a smuggled
    parameter is rejected on the wire before any query runs.
    """

    def handler(**params: object) -> ResponseEnvelope:
        # The empty bounded model rejects any unknown parameter before a
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
    """Build the ``get_data_sources`` :class:`ToolSpec`.

    ``registry`` is the source posture (enabled/disabled) as loaded from the machine
    registry at startup and held for the process lifetime -- a ``source
    enable``/``disable`` run against a live server is reflected only after a
    restart, matching the active-build refresh policy. The service annotates each
    source with its active snapshot per region from the read-only ``get_conn`` build
    (and degrades to the registry-only projection when no build is promoted). The
    projection is the single ``registry.public_view`` allowlist -- no secrets,
    local paths, OAuth config, or policy notes reach the client. Read-only
    for the shared registry both transports use.
    """

    def handler(**params: object) -> ResponseEnvelope:
        GetDataSourcesInput.model_validate(params)
        # The registry lives in memory; the active build only *enriches* each source
        # with its latest snapshot. So a missing/unpromoted build degrades to the
        # registry-only projection (conn=None) rather than failing closed -- the
        # source/license/attribution posture (PRD sections 10.7, 13.10) stays reachable
        # before any build exists.
        return run_registry_guarded(
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
