"""MCP tool implementations (§T32+).

Each module bridges a bounded input model (§T30 -- the §V18/§V19 gate) and a
shared domain service (§T31/§T17...) to the typed response envelope (§T29 -- one
§V23 status per result), and exposes a ``build_*_spec`` factory that registers
into the single shared
:class:`~arknights_mcp.mcp.tool_registry.ToolRegistry` (§V14). A tool owns no
query logic of its own -- only the model -> service -> envelope mapping -- so
both transports dispatch the exact same read-only (§V2) behaviour.
"""
