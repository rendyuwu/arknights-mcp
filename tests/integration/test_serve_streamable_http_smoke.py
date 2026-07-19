"""§T51 Streamable HTTP end-to-end smoke over a real loopback socket.

Drives the transport exactly as a remote MCP host would: bind the built ASGI app
with uvicorn on an ephemeral loopback port, then run the MCP handshake --
``initialize`` -> ``tools/list`` -> ``tools/call`` -- through the SDK's
``streamable_http_client`` over real HTTP.

This is §T51's runnable proof, covering what the in-process app test cannot:

* the Streamable HTTP transport reuses the shared core (§V14): serverInfo + the
  shared instructions + the identical tool set come back over the wire, matching
  what ``stdio`` serves;
* ``POST /mcp`` speaks the MCP protocol (§I.api);
* a factual call returns a typed ``ok`` envelope with region provenance
  (§V5/§V23) as structured content -- one domain path, both transports.

Offline + deterministic: the active build is promoted from the pinned 4-4 fixture
via the real ``import`` path, so no network is touched (§V1). TLS is the reverse
proxy's job (§I.api); the process speaks plain HTTP on loopback.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import anyio
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from arknights_mcp.app import build_application
from arknights_mcp.cli import main
from arknights_mcp.config import load_config
from arknights_mcp.instructions import SERVER_INSTRUCTIONS
from arknights_mcp.transports.streamable_http import build_asgi_app

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: The read-only tool set the shared registry exposes (§V14) -- the streamable-http
#: server must enumerate exactly this over the wire, identical to stdio.
_EXPECTED_TOOLS = frozenset(
    {
        "search_entities",
        "search_stages",
        "get_stage",
        "get_enemy",
        "get_operator",
        "compare_operator_modules",
        "analyze_stage",
    }
)


def _write_config(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.toml"
    config.write_text(
        "[database]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'current_manifest = "{(data_dir / "current.json").as_posix()}"\n'
        "\n[source_registry]\n"
        f'machine_registry = "{REGISTRY.as_posix()}"\n',
        encoding="utf-8",
    )
    return config


def _promote_fixture_build(config: Path) -> None:
    rc = main(
        ["--config", str(config), "import", "--server", "en", "--source-path", str(FIXTURE_ROOT)]
    )
    assert rc == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _drive(url: str) -> None:
    with anyio.fail_after(60):
        async with streamable_http_client(url) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                init = await session.initialize()
                # §V14; PRD §13.1: shared serverInfo + instructions, same as stdio.
                assert init.serverInfo.name == "arknights-mcp"
                assert init.instructions == SERVER_INSTRUCTIONS

                listed = await session.list_tools()
                assert {t.name for t in listed.tools} == _EXPECTED_TOOLS
                for tool in listed.tools:
                    # §V2/§V28: every exposed tool is read-only over the wire.
                    assert tool.annotations is not None
                    assert tool.annotations.readOnlyHint is True

                result = await session.call_tool(
                    "get_enemy", {"server": "en", "game_id": "enemy_1007_slime"}
                )
                # A domain result, not a protocol error.
                assert result.isError is False
                envelope = result.structuredContent
                assert envelope is not None
                assert envelope["status"] == "ok"
                assert envelope["schema_version"] == "0.1"
                # §V5: a factual result carries region provenance; en is not mixed.
                provenance = envelope["provenance"]
                assert provenance and provenance[0]["server"] == "en"


def test_serve_streamable_http_initialize_list_call(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    _promote_fixture_build(config)
    core = build_application(load_config(config))
    port = _free_port()
    app = build_asgi_app(core, path="/mcp")
    # lifespan="on": the session manager's task group must run for the app's
    # lifetime or handle_request raises (SDK contract).
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"
        anyio.run(_drive, f"http://127.0.0.1:{port}/mcp")
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        # The read-only connection is opened lazily inside the server thread on the
        # first tool call; SQLite handles are thread-bound, so it is not closed from
        # this (main) thread. In production ``server.run()`` blocks the same thread
        # that opens it, so cli/serve.py's close runs there. The test process exit
        # reclaims the fd.
