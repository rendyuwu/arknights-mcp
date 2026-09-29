"""Single MCP tool registry shared by both transports.

The registry is the one place tools are declared, so ``stdio`` and Streamable
HTTP dispatch the exact same set of tools with the exact same handlers --
no per-transport tool list. Each :class:`ToolSpec` records the wire contract
(name, title, description, JSON input schema) plus its handler, and stamps a
read-only annotation (``readOnlyHint=True``) on the emitted ``mcp.types.Tool``.

Read-only is enforced, not merely hinted: v0.1 MCP tools are read-only and
admin/mutating operations are CLI-only, so :meth:`ToolRegistry.register`
refuses any spec that is not read-only. Actual tool specs are added by their
owning modules (search/get/analyze); :func:`build_default_registry` returns the
empty shared registry they populate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mcp.types import Tool, ToolAnnotations

from arknights_mcp.mcp.envelopes import ResponseEnvelope, envelope_output_schema

#: A tool handler: called with validated keyword params, returns an envelope.
#: The concrete parameter set is per-tool; the shared contract is the return
#: type (every tool result is a typed :class:`ResponseEnvelope`).
ToolHandler = Callable[..., ResponseEnvelope]

#: Empty-object JSON schema for a tool that takes no parameters.
_EMPTY_INPUT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}

#: The tool-description budget, as a NUMBER.
#:
#: The rule used to bound the description only qualitatively ("short sentences, not a
#: 6-8-line clause chain"), so it could be closed by halving ONE tool: ``get_operator``
#: went 2185 -> 2040 and the rule read satisfied while ``get_stage``, never audited, sat at
#: 2881 and became the new longest string on the server ("the halving moved the
#: crown, not the problem"). A qualitative bound audits whichever tool someone happens to
#: look at; a number audits all of them.
#:
#: Enforced at registration, not merely asserted in a test, for the same reason read-only
#: is enforced here rather than hinted: a spec that only a test knows about is a spec a new
#: tool can ship past. Over-budget fails LOUDLY at assembly, on every transport, before any
#: client sees a truncated string.
#:
#: 1600 is a chosen budget, not a measured client limit -- no MCP client publishes where it
#: truncates a tool listing, and the client that reported the symptom reported a
#: description cut mid-sentence, not a threshold. It is set below every tool's
#: pre-pass length except the three metadata tools, so it bites, and it leaves each tool
#: room for the rule-mandated text that legitimately belongs pre-call. Going over is NOT
#: closed by deleting a mandated fact: the fact MOVES to a named home -- the
#: published input schema, a static response-side legend, a limitation,
#: or an MCP resource -- which is how the budget brought all 13 under it.
MAX_TOOL_DESCRIPTION_CHARS = 1600


class ToolRegistryError(ValueError):
    """Raised on an invalid registration (duplicate name or mutating tool)."""


@dataclass(frozen=True)
class ToolSpec:
    """One registered MCP tool: its wire contract + handler.

    ``input_schema`` is a JSON Schema object describing the tool's parameters
    (bounded Pydantic models generate it). ``read_only`` must stay
    ``True`` for v0.1; it becomes the ``readOnlyHint`` annotation.

    ``output_schema`` defaults to the shared envelope schema -- every tool
    returns the same :class:`~arknights_mcp.mcp.envelopes.ResponseEnvelope`, so the
    contract is declared once and published on every tool. Without it ``tools/list``
    never tells a client that results carry structured output.
    """

    name: str
    title: str
    description: str
    handler: ToolHandler
    input_schema: dict[str, Any] = field(default_factory=lambda: dict(_EMPTY_INPUT_SCHEMA))
    read_only: bool = True
    output_schema: dict[str, Any] = field(default_factory=envelope_output_schema)

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
            outputSchema=self.output_schema,
            annotations=self.annotations(),
        )


class ToolRegistry:
    """The shared, order-preserving registry of MCP tools.

    Registration is closed to read-only tools: a mutating spec is
    rejected at registration, so no admin operation can leak onto the MCP
    surface. Names are unique; lookup + listing back the transport dispatch.
    """

    def __init__(self) -> None:
        # Insertion order is preserved so ``list_tools`` output is deterministic.
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        """Register ``spec``. Rejects a duplicate name, a mutating tool, or an
        over-budget description."""
        if not spec.read_only:
            raise ToolRegistryError(
                f"tool {spec.name!r} is not read-only; MCP tools are read-only "
                "and admin ops are CLI-only"
            )
        if len(spec.description) > MAX_TOOL_DESCRIPTION_CHARS:
            raise ToolRegistryError(
                f"tool {spec.name!r} description is {len(spec.description)} chars, over the "
                f"{MAX_TOOL_DESCRIPTION_CHARS}-char budget. Move a fact to "
                "its named home -- input schema, response legend, a "
                "limitation, or an MCP resource -- never delete a mandated one"
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
    """Build the shared registry both transports use.

    Empty at import; the search/get/analyze tool modules register their specs here so
    there is a single tool set with a single home.
    """
    return ToolRegistry()
