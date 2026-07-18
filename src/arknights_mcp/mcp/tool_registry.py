"""Single MCP tool registry shared by both transports (§T29; §V14).

The registry is the one place tools are declared, so ``stdio`` and Streamable
HTTP dispatch the exact same set of tools with the exact same handlers (§V14) --
no per-transport tool list. Each :class:`ToolSpec` records the wire contract
(name, title, description, JSON input schema) plus its handler, and stamps a
read-only annotation (``readOnlyHint=True``) on the emitted ``mcp.types.Tool``.

Read-only is enforced, not merely hinted: v0.1 MCP tools are read-only (§V2) and
admin/mutating operations are CLI-only (§V28), so :meth:`ToolRegistry.register`
refuses any spec that is not read-only. Actual tool specs are added by their
owning §T tasks (search/get/analyze); :func:`build_default_registry` returns the
empty shared registry they populate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mcp.types import Tool, ToolAnnotations

from arknights_mcp.mcp.envelopes import ResponseEnvelope

#: A tool handler: called with validated keyword params, returns an envelope.
#: The concrete parameter set is per-tool; the shared contract is the return
#: type (every tool result is a typed :class:`ResponseEnvelope`, §V23).
ToolHandler = Callable[..., ResponseEnvelope]

#: Empty-object JSON schema for a tool that takes no parameters.
_EMPTY_INPUT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


class ToolRegistryError(ValueError):
    """Raised on an invalid registration (duplicate name or mutating tool)."""


@dataclass(frozen=True)
class ToolSpec:
    """One registered MCP tool: its wire contract + handler (§V14).

    ``input_schema`` is a JSON Schema object describing the tool's parameters
    (bounded Pydantic models generate it in §T30). ``read_only`` must stay
    ``True`` for v0.1 (§V2/§V28); it becomes the ``readOnlyHint`` annotation.
    """

    name: str
    title: str
    description: str
    handler: ToolHandler
    input_schema: dict[str, Any] = field(default_factory=lambda: dict(_EMPTY_INPUT_SCHEMA))
    read_only: bool = True

    def annotations(self) -> ToolAnnotations:
        """MCP behaviour hints. v0.1 tools are read-only + non-destructive."""
        return ToolAnnotations(
            title=self.title,
            readOnlyHint=self.read_only,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )

    def to_mcp_tool(self) -> Tool:
        """Project this spec to the ``mcp.types.Tool`` sent over the wire."""
        return Tool(
            name=self.name,
            title=self.title,
            description=self.description,
            inputSchema=self.input_schema,
            annotations=self.annotations(),
        )


class ToolRegistry:
    """The shared, order-preserving registry of MCP tools (§V14).

    Registration is closed to read-only tools (§V2/§V28): a mutating spec is
    rejected at registration, so no admin operation can leak onto the MCP
    surface. Names are unique; lookup + listing back the transport dispatch.
    """

    def __init__(self) -> None:
        # Insertion order is preserved so ``list_tools`` output is deterministic.
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        """Register ``spec``. Rejects a duplicate name or a mutating tool."""
        if not spec.read_only:
            raise ToolRegistryError(
                f"tool {spec.name!r} is not read-only; MCP tools are read-only (§V2) "
                "and admin ops are CLI-only (§V28)"
            )
        if spec.name in self._specs:
            raise ToolRegistryError(f"tool {spec.name!r} already registered")
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        """Return the spec for ``name`` or raise :class:`KeyError`."""
        return self._specs[name]

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def names(self) -> tuple[str, ...]:
        """Registered tool names, in registration order."""
        return tuple(self._specs)

    def specs(self) -> tuple[ToolSpec, ...]:
        """Registered specs, in registration order."""
        return tuple(self._specs.values())

    def to_mcp_tools(self) -> list[Tool]:
        """All specs projected to ``mcp.types.Tool`` (for ``list_tools``)."""
        return [spec.to_mcp_tool() for spec in self._specs.values()]


def build_default_registry() -> ToolRegistry:
    """Build the shared registry both transports use (§V14).

    Empty at §T29; the search/get/analyze tool tasks register their specs here so
    there is a single tool set with a single home.
    """
    return ToolRegistry()
