"""Optional ``arknights://`` MCP resources (§T37; §V27; §I.resource; PRD §13.11).

Resources are a second, read-only projection of the same intel the tools expose:
a client can address one entity by a stable URI instead of a tool call. Every
resource is a *point* address -- one enemy, one stage, one region's status, or the
public source registry -- never a bulk enumeration or a raw dump (§V19; PRD §13.11).

The surface mirrors PRD §13.11 / §I.resource::

    arknights://enemy/{server}/{game_id}    (template)
    arknights://stage/{server}/{stage_id}   (template)
    arknights://status/{server}             (template)
    arknights://sources                     (fixed)

``arknights://operator/{server}/{game_id}`` is intentionally **not** registered
yet: the operator intel service is a stub until ``get_operator`` (§T44, M4), and a
resource whose reads always fail is worse than an absent one. It is added
alongside §T44.

Four invariants shape this module:

* **§V14 / §V37** -- resources own no query logic. Entity resources dispatch
  through the exact ``get_enemy`` / ``get_stage`` tool handlers, and the metadata
  resources call the shared ``get_data_status`` / ``get_data_sources`` services, so
  both surfaces (tool + resource) run identical domain code with no duplicated
  shaping. A resource read returns the same typed
  :class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope`, serialized as the
  resource body.
* **§V27** -- ``arknights://sources`` routes through ``get_data_sources`` ->
  ``registry.public_view()``: it never re-enumerates the allowlist, so it cannot
  leak secrets, local paths, OAuth config, or policy notes.
* **§V5 / §V23** -- every factual body carries region + provenance and a typed
  status; a bad region in a URI fails closed to an ``unsupported_server`` envelope,
  a missing/over-long id to ``not_found`` -- never a leaked exception or path.
* **§V2** -- read-only throughout: the services only read, through the shared
  read-only connection, and admin ops stay CLI-only (§V28).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import unquote

from mcp.types import ReadResourceResult, Resource, ResourceTemplate, TextResourceContents
from pydantic import ValidationError

from arknights_mcp.mcp.envelopes import (
    Provenance,
    ResponseEnvelope,
    ToolStatus,
    build_envelope,
    error,
    ok,
)
from arknights_mcp.mcp.tool_registry import ToolHandler
from arknights_mcp.mcp.tools._shared import ConnectionProvider, run_guarded
from arknights_mcp.mcp.tools.enemy import build_get_enemy_spec
from arknights_mcp.mcp.tools.stage import build_get_stage_spec
from arknights_mcp.services.source_status import DataSourcesResult, get_data_sources
from arknights_mcp.services.status import DataStatus, get_data_status
from arknights_mcp.sources.registry import SourceRegistry

#: JSON mime for every resource body -- a typed envelope, the same shape a tool
#: returns (§V14: one result contract across both surfaces).
_JSON_MIME = "application/json"

#: §V5 supported regions as a runtime set (mirrors ``models.common.Region``).
_REGIONS = frozenset({"en", "cn"})

#: ``arknights://`` URIs mirror PRD §13.11 / §I.resource verbatim.
_ENEMY_TEMPLATE = "arknights://enemy/{server}/{game_id}"
_STAGE_TEMPLATE = "arknights://stage/{server}/{stage_id}"
_STATUS_TEMPLATE = "arknights://status/{server}"
_SOURCES_URI = "arknights://sources"

#: Fixed, safe copy for the typed failure envelopes (§V23 -- no echo of untrusted
#: URI input, no stack trace, no local path).
_UNSUPPORTED_SERVER_MESSAGE = "the requested region is not supported"
_UNSUPPORTED_SERVER_ACTION = "use a supported region: en or cn"
_NOT_FOUND_MESSAGE = "no entity matched the requested resource uri"
_NOT_FOUND_ACTION = (
    "verify the region + id, or run `arknights-mcp status` to check the active build"
)

#: Region-scoped staleness copy (§V5): a region with no active snapshot is stale
#: for that region even when another region keeps the build globally non-empty.
_NO_REGION_DATA_MESSAGE = "no active snapshot for the requested region in the active build"
_NO_REGION_DATA_ACTION = "run `arknights-mcp sync --server <region>` or `arknights-mcp import`"

#: A resource handler: URI-captured params -> a typed envelope (the resource body).
ResourceHandler = Callable[[Mapping[str, str]], ResponseEnvelope]

#: Matches a ``{name}`` placeholder segment in a URI template.
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


class ResourceError(ValueError):
    """Raised on an invalid registration or an unknown resource URI."""


@dataclass(frozen=True)
class ResourceSpec:
    """One registered ``arknights://`` resource: its wire contract + handler.

    A spec whose ``uri_template`` carries a ``{placeholder}`` is a *template*
    (advertised via ``list_resource_templates``); one without is a *fixed* resource
    (``list_resources``). ``handler`` maps the URI-captured params to the typed
    :class:`ResponseEnvelope` that becomes the resource body (§V14/§V23).
    """

    name: str
    title: str
    description: str
    uri_template: str
    handler: ResourceHandler
    mime_type: str = _JSON_MIME

    @property
    def is_template(self) -> bool:
        """True when the URI carries a ``{placeholder}`` (a template, not fixed)."""
        return "{" in self.uri_template

    def to_mcp_resource(self) -> Resource:
        """Project a *fixed* resource to the ``mcp.types.Resource`` on the wire."""
        if self.is_template:
            raise ResourceError(f"resource {self.name!r} is a template, not a fixed resource")
        return Resource(
            name=self.name,
            title=self.title,
            uri=self.uri_template,  # type: ignore[arg-type]  # AnyUrl coerces the str
            description=self.description,
            mimeType=self.mime_type,
        )

    def to_mcp_resource_template(self) -> ResourceTemplate:
        """Project a *template* resource to the ``mcp.types.ResourceTemplate``."""
        if not self.is_template:
            raise ResourceError(f"resource {self.name!r} is fixed, not a template")
        return ResourceTemplate(
            name=self.name,
            title=self.title,
            uriTemplate=self.uri_template,
            description=self.description,
            mimeType=self.mime_type,
        )


def _compile(template: str) -> re.Pattern[str]:
    """Compile a URI template to a full-match regex, one group per placeholder.

    A ``{name}`` becomes a ``(?P<name>[^/]+)`` group (a single path segment, so a
    crafted id cannot span extra segments); literal text is escaped. A template
    with no placeholder compiles to an exact-match pattern (a fixed resource).
    """
    parts: list[str] = []
    last = 0
    for match in _PLACEHOLDER.finditer(template):
        parts.append(re.escape(template[last : match.start()]))
        parts.append(f"(?P<{match.group(1)}>[^/]+)")
        last = match.end()
    parts.append(re.escape(template[last:]))
    return re.compile("^" + "".join(parts) + "$")


class ResourceRegistry:
    """The shared, order-preserving registry of ``arknights://`` resources (§V14).

    Both transports would list + read through this one registry, so a resource is
    served identically regardless of transport. Names and URIs are unique; ``read``
    matches a URI against the registered templates and dispatches to the handler.
    """

    def __init__(self) -> None:
        # Insertion order preserved so listings are deterministic.
        self._specs: dict[str, ResourceSpec] = {}
        self._matchers: dict[str, re.Pattern[str]] = {}
        self._uris: set[str] = set()

    def register(self, spec: ResourceSpec) -> ResourceSpec:
        """Register ``spec``. Rejects a duplicate name or URI template."""
        if spec.name in self._specs:
            raise ResourceError(f"resource {spec.name!r} already registered")
        if spec.uri_template in self._uris:
            raise ResourceError(f"resource uri {spec.uri_template!r} already registered")
        self._specs[spec.name] = spec
        self._matchers[spec.name] = _compile(spec.uri_template)
        self._uris.add(spec.uri_template)
        return spec

    def names(self) -> tuple[str, ...]:
        """Registered resource names, in registration order."""
        return tuple(self._specs)

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def list_resources(self) -> list[Resource]:
        """Fixed resources projected to ``mcp.types.Resource`` (``list_resources``)."""
        return [s.to_mcp_resource() for s in self._specs.values() if not s.is_template]

    def list_resource_templates(self) -> list[ResourceTemplate]:
        """Template resources projected for ``list_resource_templates``."""
        return [s.to_mcp_resource_template() for s in self._specs.values() if s.is_template]

    def read(self, uri: str) -> ReadResourceResult:
        """Read ``uri`` -> a ``ReadResourceResult`` carrying the typed envelope body.

        A URI that matches a registered template dispatches to its handler; the
        returned envelope (``ok`` / ``not_found`` / ``unsupported_server`` / ...) is
        serialized as the JSON body. A URI matching no resource is an unknown
        resource: :class:`ResourceError` (never a raw dump or a scan of others).
        """
        for name, matcher in self._matchers.items():
            captured = matcher.match(uri)
            if captured is None:
                continue
            params = {key: unquote(value) for key, value in captured.groupdict().items()}
            envelope = self._specs[name].handler(params)
            body = json.dumps(envelope.to_dict())
            return ReadResourceResult(
                contents=[
                    TextResourceContents(
                        uri=uri,  # type: ignore[arg-type]  # AnyUrl coerces the str
                        mimeType=self._specs[name].mime_type,
                        text=body,
                    )
                ]
            )
        raise ResourceError("no resource matches the requested uri")


def _unsupported_region(params: Mapping[str, str]) -> ResponseEnvelope | None:
    """Fail closed to ``unsupported_server`` when the URI region is not en/cn (§V5).

    Returns the envelope to short-circuit with, or ``None`` when the region is
    valid. The fixed message never echoes the untrusted URI value (§V23).
    """
    if params.get("server") not in _REGIONS:
        return error(
            "unsupported_server",
            _UNSUPPORTED_SERVER_MESSAGE,
            suggested_action=_UNSUPPORTED_SERVER_ACTION,
        )
    return None


def _make_entity_handler(tool_handler: ToolHandler, *, uri_id_key: str) -> ResourceHandler:
    """Bridge an entity URI to the ``get_enemy`` / ``get_stage`` tool handler (§V14).

    The tool handler owns the region + id validation, the lookup, and the §V5/§V23
    shaping, so the resource adds no domain logic (§V37). A bad region short-circuits
    to ``unsupported_server``; an over-long/empty id trips the tool's bounded model,
    which we map to ``not_found`` (a URI that cannot address a real entity), never a
    leaked ``ValidationError``.
    """

    def handler(params: Mapping[str, str]) -> ResponseEnvelope:
        guard = _unsupported_region(params)
        if guard is not None:
            return guard
        try:
            return tool_handler(server=params["server"], game_id=params[uri_id_key])
        except ValidationError:
            return error("not_found", _NOT_FOUND_MESSAGE, suggested_action=_NOT_FOUND_ACTION)

    return handler


def _make_status_handler(get_conn: ConnectionProvider, mode: str) -> ResourceHandler:
    """Region-scoped ``arknights://status/{server}`` over ``get_data_status`` (§V14).

    Reuses the shared status service (no re-query here) and scopes its result to the
    URI region: only that region's snapshots are returned, and each contributes
    provenance (§V5). A ``data_stale`` service result maps to the ``data_stale``
    envelope status; the DB-unavailable/internal guard is the shared one (§V37).
    """

    def handler(params: Mapping[str, str]) -> ResponseEnvelope:
        guard = _unsupported_region(params)
        if guard is not None:
            return guard
        server = params["server"]

        def shape(status: DataStatus) -> ResponseEnvelope:
            snapshots = tuple(s for s in status.snapshots if s.server == server)
            provenance = tuple(
                Provenance(server=s.server, snapshot_id=s.snapshot_id, imported_at=s.imported_at)
                for s in snapshots
            )
            # Region-scope the whole verdict, not just the snapshot list: a region
            # with no active snapshot is ``data_stale`` for that region even when
            # another region keeps the build globally non-empty (§V5). Recompute
            # status/warnings/action for the region rather than leaking the global
            # verdict; keep the global build-wide warnings only when the region
            # itself has data.
            if not snapshots:
                env_status: ToolStatus = "data_stale"
                warnings: tuple[str, ...] = (_NO_REGION_DATA_MESSAGE,)
                suggested_action: str | None = _NO_REGION_DATA_ACTION
            else:
                env_status = "data_stale" if status.status == "data_stale" else "ok"
                warnings = status.warnings
                suggested_action = status.suggested_action
            # Reuse the service's serialization (§V37) and override the region-scoped
            # keys, rather than re-enumerating the status fields.
            data = dict(status.to_dict())
            data["server"] = server
            data["status"] = env_status
            data["snapshots"] = [s.to_dict() for s in snapshots]
            data["warnings"] = list(warnings)
            data["suggested_action"] = suggested_action
            return build_envelope(
                env_status,
                data=data,
                provenance=provenance,
                analyzer_version=status.analyzer_version,
            )

        return run_guarded(get_conn, lambda conn: get_data_status(conn, mode=mode), shape)

    return handler


def _make_sources_handler(
    get_conn: ConnectionProvider, registry: SourceRegistry
) -> ResourceHandler:
    """``arknights://sources`` over ``get_data_sources`` -> ``public_view`` (§V27).

    The public-safe projection is the service's single allowlist (§V34): this
    resource never re-enumerates it, so it cannot leak secrets, local paths, OAuth
    config, or policy notes. Read-only through the shared connection (§V2).
    """

    def handler(_params: Mapping[str, str]) -> ResponseEnvelope:
        def shape(result: DataSourcesResult) -> ResponseEnvelope:
            return ok(result.to_dict())

        return run_guarded(get_conn, lambda conn: get_data_sources(registry, conn), shape)

    return handler


#: PRD §13.11 resource descriptions (short, no game prose; §V16/§V18).
_ENEMY_DESCRIPTION = (
    "One Arknights enemy's facts by region + game_id: class/flags, attack + motion "
    "type, and the per-level stat block, with region + provenance. en/cn never mixed."
)
_STAGE_DESCRIPTION = (
    "One Arknights stage's facts by region + game stage id, with region + provenance. "
    "Heavy map/routes/spawns stay opt-in on the get_stage tool (bounded); en/cn never mixed."
)
_STATUS_DESCRIPTION = (
    "Active-build data status for one region: schema + analyzer version, active "
    "snapshots (source/commit/import time/age), and any staleness warnings."
)
_SOURCES_DESCRIPTION = (
    "The public-safe source registry: id, owner, canonical URL, purpose, regions, "
    "license/permission posture, and the active snapshot per region. No secrets/paths."
)


def build_default_resources(
    get_conn: ConnectionProvider,
    *,
    registry: SourceRegistry,
    mode: str = "local",
) -> ResourceRegistry:
    """Build the shared ``arknights://`` resource registry (§T37; §V14).

    ``get_conn`` returns the process-wide read-only connection to the promoted
    build; ``registry`` is the live source posture for ``arknights://sources``. Every
    registered resource is read-only (§V2) and reuses the tools/services (§V14/§V37).
    The operator resource is added with ``get_operator`` (§T44); it is absent here
    because the operator service is a stub.
    """
    enemy_handler = build_get_enemy_spec(get_conn).handler
    stage_handler = build_get_stage_spec(get_conn).handler

    resources = ResourceRegistry()
    resources.register(
        ResourceSpec(
            name="enemy",
            title="Arknights enemy",
            description=_ENEMY_DESCRIPTION,
            uri_template=_ENEMY_TEMPLATE,
            handler=_make_entity_handler(enemy_handler, uri_id_key="game_id"),
        )
    )
    resources.register(
        ResourceSpec(
            name="stage",
            title="Arknights stage",
            description=_STAGE_DESCRIPTION,
            uri_template=_STAGE_TEMPLATE,
            handler=_make_entity_handler(stage_handler, uri_id_key="stage_id"),
        )
    )
    resources.register(
        ResourceSpec(
            name="status",
            title="Arknights data status",
            description=_STATUS_DESCRIPTION,
            uri_template=_STATUS_TEMPLATE,
            handler=_make_status_handler(get_conn, mode),
        )
    )
    resources.register(
        ResourceSpec(
            name="sources",
            title="Arknights data sources",
            description=_SOURCES_DESCRIPTION,
            uri_template=_SOURCES_URI,
            handler=_make_sources_handler(get_conn, registry),
        )
    )
    return resources
