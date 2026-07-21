"""§T61 M7 local↔remote transport result parity (§V14).

§V14 is the one-core/two-transports invariant: both transports dispatch the *same*
``tool_registry`` + services, so the *same* DB + *same* input must yield an
*identical* domain result, with no duplicated domain logic across modes. The
existing smoke tests (``tests/integration/test_serve_stdio_smoke.py``,
``test_serve_streamable_http_smoke.py``) each prove *one* wire serves the shared
core; the in-process contract test proves the *registry* adds no divergent logic.
None of them compares the two *wires* against each other. This test does exactly
that: it drives both real transports against the *same promoted build* and asserts
the ``initialize`` handshake, the ``tools/list`` enumeration, and every one of the
``tools/call`` domain payloads come back byte-identical across modes.

How the two wires share one build (§V37 DRY):

* the Streamable HTTP wire is the shared :func:`tests.support.remote_harness.remote_server`
  harness -- it promotes the pinned 4-4 fixture into ``tmp_path/data`` via the real
  ``import`` CLI, writes ``tmp_path/config.toml``, and serves that build behind the
  full authenticated remote stack (real :class:`OidcTokenVerifier`, local JWKS) on
  a loopback socket. This is the genuine production remote path, not an authless
  stand-in;
* the local ``stdio`` wire is a real ``python -m arknights_mcp ... serve --transport
  stdio`` subprocess pointed at *that same* ``tmp_path/config.toml`` -- so it resolves
  the identical immutable build file from the same ``current.json``. Both processes
  open the build strictly read-only, so serving it from two processes at once is safe.

The equality assertion is a whole-snapshot ``==`` on ``{serverInfo, instructions,
tools, calls}`` per wire (dicts compare structurally). The only fields excluded are
``get_data_status``'s two call-time-derived fields -- ``data.generated_at`` (the
response timestamp) and each ``data.snapshots[*].age_days`` (``now - imported_at``):
both come from the wall clock at call time, so two calls differ *on the same wire
too*. They are not DB-derived domain data (what §V14 governs), so they are the sole
parity exclusions; the stored ``imported_at``/``snapshot_id`` provenance still must
match. The two operator tools resolve to ``not_found`` on the 4-4 build (it carries
no operators) -- a legitimate identical domain result the parity check still covers.

Offline + deterministic: the build is promoted from the pinned fixture (no network,
§V1); the OIDC keypair + JWKS are local (no provider reached, §V10).
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from tests.support.remote_harness import EXPECTED_TOOLS
from tests.support.remote_harness import remote_server as _remote_server

#: One representative call per §I.tool tool -- the exact matrix both wires dispatch.
#: Covers the region + exactly-one-of selectors, the heavy ``get_stage`` include
#: flags (map/routes/spawns, the §V22 opt-in sections), the depth-defaulted
#: ``analyze_stage`` evidence path, the two free-text searches, and the two
#: parameterless posture tools. Against the 4-4 build the seven entity/analysis/
#: posture tools resolve to a rich ``ok`` payload; the two operator tools resolve
#: to ``not_found`` (no operators in the fixture) -- both are domain results whose
#: envelope must be identical across transports.
_CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("search_entities", {"query": "slime"}),
    ("search_stages", {"query": "4-4"}),
    (
        "get_stage",
        {
            "server": "en",
            "stage_code": "4-4",
            "include_map": True,
            "include_routes": True,
            "include_spawns": True,
        },
    ),
    ("get_enemy", {"server": "en", "game_id": "enemy_1007_slime"}),
    ("get_operator", {"server": "en", "game_id": "char_002_amiya"}),
    ("compare_operator_modules", {"server": "en", "game_id": "char_002_amiya"}),
    ("analyze_stage", {"server": "en", "stage_code": "4-4"}),
    # No penguin drop cache in the promoted fixture build, so these resolve to
    # not_found -- a legitimate identical domain result across both wires (like the
    # two operator tools), which the parity check still covers.
    ("get_stage_drops", {"server": "en", "stage_code": "4-4"}),
    ("get_item_drops", {"server": "en", "game_id": "sugar"}),
    # No announcement feed in the promoted fixture build (the source is disabled by
    # default, D14/§V56), so this resolves to an empty ``ok`` list -- a legitimate
    # identical domain result across both wires, which the parity check still covers.
    ("get_announcements", {"server": "en"}),
    ("get_data_status", {}),
    ("get_data_sources", {}),
)


async def _drive(session: ClientSession) -> dict[str, Any]:
    """Run the MCP handshake + the full call matrix; return a comparable snapshot.

    The returned dict captures every observable both transports must agree on:
    the ``initialize`` serverInfo + shared instructions, the ``tools/list``
    enumeration (each tool's full wire spec via ``model_dump``, name-sorted so the
    comparison is order-independent), and the structured-content envelope of every
    ``tools/call``. A call that surfaces a *protocol* error (rather than a typed
    domain envelope) fails here -- a degraded domain result is carried in the
    envelope, never as ``isError`` (§V23).
    """
    init = await session.initialize()
    listed = await session.list_tools()
    tools = sorted((t.model_dump(mode="json") for t in listed.tools), key=lambda d: d["name"])

    calls: dict[str, dict[str, Any]] = {}
    for name, args in _CALLS:
        result = await session.call_tool(name, args)
        assert result.isError is False, f"{name} surfaced a protocol error, not a domain result"
        assert result.structuredContent is not None, f"{name} returned no structuredContent"
        calls[name] = result.structuredContent

    return {
        "serverInfo": {"name": init.serverInfo.name, "version": init.serverInfo.version},
        "instructions": init.instructions,
        "tools": tools,
        "calls": calls,
    }


async def _remote_snapshot(url: str, token: str) -> dict[str, Any]:
    """Drive the authenticated Streamable HTTP wire (§I.api; §V10 bearer)."""
    http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    with anyio.fail_after(90):
        async with (
            http_client,
            streamable_http_client(url, http_client=http_client) as (read_stream, write_stream, _),
        ):
            async with ClientSession(read_stream, write_stream) as session:
                return await _drive(session)


async def _local_snapshot(config: Path, cwd: Path) -> dict[str, Any]:
    """Drive the local ``stdio`` wire as a real ``serve`` subprocess (§V13)."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "arknights_mcp", "--config", str(config), "serve", "--transport", "stdio"],
        cwd=str(cwd),
        env=dict(os.environ),
    )
    with anyio.fail_after(90):
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                return await _drive(session)


def _strip_volatile(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy with ``get_data_status``'s call-time fields removed.

    ``data.generated_at`` (response timestamp) and each ``data.snapshots[*].age_days``
    (``now - imported_at``) are derived from the wall clock at call time -- two calls
    differ on the *same* transport too, so they are not a cross-transport parity
    signal and are the only fields excluded (§V14 governs the DB-derived payload).
    Everything else, including the stored ``imported_at``/``snapshot_id`` provenance,
    stays in the comparison.
    """
    cleaned = copy.deepcopy(snapshot)
    status_envelope = cleaned["calls"].get("get_data_status")
    if status_envelope is not None:
        data = status_envelope.get("data", {})
        data.pop("generated_at", None)
        for snap in data.get("snapshots", []):
            snap.pop("age_days", None)
    return cleaned


def test_call_matrix_covers_every_tool() -> None:
    # Guard: the parity matrix must exercise the full §I.tool set of nine, so a tool
    # added later without a parity case fails here rather than silently going
    # uncompared. EXPECTED_TOOLS is the one shared source of the registry's names.
    assert {name for name, _ in _CALLS} == EXPECTED_TOOLS


def test_local_and_remote_transports_are_result_identical(tmp_path: Path) -> None:
    # §V14: the two transports dispatch the same registry + services, so the same
    # promoted build + same inputs must yield identical results. Drive both real
    # wires against the one build the harness promotes and assert the whole snapshot
    # (serverInfo + shared instructions + tools/list + all nine tools/call payloads)
    # matches, modulo get_data_status's two call-time-derived fields.
    config = tmp_path / "config.toml"  # written + promoted by remote_server

    with _remote_server(tmp_path) as (url, issuer):
        assert config.exists(), "remote_server did not write the shared config.toml"
        remote = anyio.run(_remote_snapshot, url, issuer.mint())

    # The stdio subprocess reads the same immutable build (read-only) from the same
    # current.json; run it after the HTTP server tears down (the build file persists).
    local = anyio.run(_local_snapshot, config, tmp_path)

    local_clean = _strip_volatile(local)
    remote_clean = _strip_volatile(remote)

    # Handshake + enumeration parity: same serverInfo, same shared instructions,
    # same tool set with identical wire specs (name/title/description/schema/hints).
    assert local_clean["serverInfo"] == remote_clean["serverInfo"]
    assert local_clean["instructions"] == remote_clean["instructions"]
    assert local_clean["tools"] == remote_clean["tools"]

    # Per-tool domain-payload parity, asserted individually so a divergence names
    # the offending tool rather than dumping the whole snapshot.
    assert set(local_clean["calls"]) == EXPECTED_TOOLS == set(remote_clean["calls"])
    for name in sorted(EXPECTED_TOOLS):
        assert local_clean["calls"][name] == remote_clean["calls"][name], (
            f"transport parity divergence in tool {name!r}"
        )

    # And the whole snapshot as one object, so nothing outside the per-key checks
    # above (a field added to the envelope later) escapes the comparison.
    assert local_clean == remote_clean
