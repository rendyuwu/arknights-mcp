"""§T47 fresh-process install smoke: ``serve --transport stdio`` end-to-end.

Drives the packaged server exactly as an MCP host would: spawn the installed
package as a subprocess (``python -m arknights_mcp ... serve --transport stdio``,
the module twin of the ``arknights-mcp`` console script) and run the two-call MCP
handshake over a *real* stdio pipe -- ``initialize`` then ``tools/list`` then
``tools/call`` -- via the MCP client SDK.

This is the milestone's runnable proof, covering what an in-process registry test
(``tests/contract``) cannot:

* the ``serve`` command wires the shared core to the stdio transport (§V14) and
  the process actually starts from locked deps;
* stdout carries *only* the framed MCP protocol (§V13) -- the client's JSON-RPC
  parse would fail on any stray print, so a successful handshake is the check;
* a factual call returns a typed ``ok`` envelope with region provenance (§V5/§V23)
  as structured content.

Offline + deterministic: the active build is promoted from the pinned 4-4 fixture
via the real ``import`` path, so no network is touched (§V1).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from arknights_mcp.cli import main
from arknights_mcp.instructions import SERVER_INSTRUCTIONS

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: The read-only tool set the shared registry exposes (§V14) -- the stdio server
#: must enumerate exactly this over the wire.
_EXPECTED_TOOLS = frozenset(
    {
        "search_entities",
        "search_stages",
        "get_stage",
        "get_enemy",
        "get_operator",
        "compare_operator_modules",
        "analyze_stage",
        "get_stage_drops",
        "get_data_status",
        "get_data_sources",
    }
)


def _write_config(tmp_path: Path) -> tuple[Path, Path]:
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
    return config, data_dir


def _promote_fixture_build(config: Path) -> None:
    """Build + promote the pinned 4-4 fixture so the served DB has a live target."""
    rc = main(
        ["--config", str(config), "import", "--server", "en", "--source-path", str(FIXTURE_ROOT)]
    )
    assert rc == 0


async def _drive(config: Path, cwd: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "arknights_mcp", "--config", str(config), "serve", "--transport", "stdio"],
        cwd=str(cwd),
        env=dict(os.environ),
    )
    # A stray stdout write (a §V13 violation) corrupts the JSON-RPC stream, so any
    # step below raises rather than passing -- the handshake is the stdout check.
    with anyio.fail_after(60):
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                init = await session.initialize()
                # serverInfo + the shared instructions string (§V14; PRD §13.1).
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


def test_serve_stdio_initialize_list_call(tmp_path: Path) -> None:
    config, _ = _write_config(tmp_path)
    _promote_fixture_build(config)
    anyio.run(_drive, config, tmp_path)


async def _drive_no_db(config: Path, cwd: Path) -> str:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "arknights_mcp", "--config", str(config), "serve", "--transport", "stdio"],
        cwd=str(cwd),
        env=dict(os.environ),
    )
    with anyio.fail_after(60):
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "get_enemy", {"server": "en", "game_id": "enemy_1007_slime"}
                )
                envelope = result.structuredContent
                assert envelope is not None
                return str(envelope["status"])


def test_serve_starts_without_a_promoted_build(tmp_path: Path) -> None:
    # §V23: the server starts even with nothing promoted; tools fail closed to a
    # typed database_unavailable result rather than the process refusing to boot.
    config, _ = _write_config(tmp_path)
    status = anyio.run(_drive_no_db, config, tmp_path)
    assert status == "database_unavailable"
