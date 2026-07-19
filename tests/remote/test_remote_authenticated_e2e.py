"""§T56 authenticated remote validation over a real loopback socket (§V14).

The stand-in for the manual "MCP Inspector / Claude connector / OpenAI API"
validation (:doc:`../../docs/clients/remote`): those need a public HTTPS endpoint +
a live OIDC provider + accounts, so they cannot run in CI. This is the runnable
half -- it drives the *full auth-requiring remote stack* over a real HTTP socket,
exactly as a remote MCP host would, and proves the one machine-checkable claim T56
rests on: an authenticated request over the Streamable HTTP wire is served by the
*same shared core* ``stdio`` serves (§V14), and an unauthenticated one is refused.

What this covers that the in-process middleware/isolation unit tests cannot:

* the request threads the *composed* remote stack (redacted logging → bearer →
  rate/concurrency → request limits → session manager) end to end, over uvicorn,
  through the SDK client -- not a stub inner app;
* the bearer is validated by the *real* :class:`OidcTokenVerifier` decode path
  (RS256 signature + iss/aud/exp/scope, §V10) -- only the JWKS key fetch is local
  (:class:`~tests.support.oidc_issuer.LocalOidcIssuer`), so the honest-token path is
  genuinely exercised, not stubbed;
* over that authenticated wire the server returns the shared serverInfo + shared
  instructions + the identical 7-tool set + a typed ``ok`` envelope with ``en``
  provenance -- matching what ``stdio`` serves (§V14/§V5/§V23).

The adversarial auth matrix (expired / wrong-issuer / wrong-aud / insufficient
scope / cross-principal isolation / log scan) is §T57's; this asserts only the
happy path *and* that a missing bearer is refused, so the validation is meaningful
without duplicating T57.

Offline + deterministic: the active build is promoted from the pinned 4-4 fixture
via the real ``import`` path (no network, §V1); the OIDC keypair + JWKS are local
(no provider reached). TLS is the reverse proxy's job (§I.api); the process speaks
plain HTTP on loopback, with ``behind_proxy`` semantics enforced in the app layer.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import anyio
import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from tests.support.oidc_issuer import LocalOidcIssuer

from arknights_mcp.app import build_application
from arknights_mcp.auth.oidc import OidcTokenVerifier
from arknights_mcp.cli import main
from arknights_mcp.config import load_config
from arknights_mcp.instructions import SERVER_INSTRUCTIONS
from arknights_mcp.transports.streamable_http import build_asgi_app, wrap_remote_app

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: The read-only tool set the shared registry exposes (§V14) -- the authenticated
#: remote server must enumerate exactly this over the wire, identical to stdio.
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


@pytest.fixture
def remote_server(tmp_path: Path) -> Iterator[tuple[str, LocalOidcIssuer]]:
    """Serve the fixture build behind the full auth-requiring remote stack.

    Wraps the shared ASGI app in :func:`wrap_remote_app` with a real
    :class:`OidcTokenVerifier` whose JWKS key is resolved from a local issuer, then
    binds uvicorn on an ephemeral loopback port. Yields the ``/mcp`` URL and the
    issuer (so the test mints a bearer the verifier will accept).
    """
    config_path = _write_config(tmp_path)
    _promote_fixture_build(config_path)
    config = load_config(config_path)
    core = build_application(config)

    issuer = LocalOidcIssuer()
    verifier = OidcTokenVerifier(issuer.settings, jwks_client=issuer.jwks_resolver)
    app = build_asgi_app(core, path="/mcp")
    # The full §T54 stack: redacted logging → bearer (§V10) → rate/concurrency →
    # request limits (§V11) → session manager. Real verifier, local JWKS.
    wrapped = wrap_remote_app(app, config, verifier, issuer.settings)

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(wrapped, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"
        yield f"http://127.0.0.1:{port}/mcp", issuer
    finally:
        server.should_exit = True
        thread.join(timeout=15)


async def _drive_authenticated(url: str, token: str) -> None:
    """Run the MCP handshake over the authenticated wire; assert §V14 identity.

    The bearer is carried on a caller-built ``httpx.AsyncClient`` (the SDK's
    ``streamable_http_client`` reads auth/headers off a provided client and leaves
    its lifecycle to us -- hence the explicit ``async with`` closing it).
    """
    http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    with anyio.fail_after(60):
        async with (
            http_client,
            streamable_http_client(url, http_client=http_client) as (
                read_stream,
                write_stream,
                _sid,
            ),
        ):
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
                # A domain result over the authenticated wire, not a protocol error.
                assert result.isError is False
                envelope = result.structuredContent
                assert envelope is not None
                assert envelope["status"] == "ok"
                assert envelope["schema_version"] == "0.1"
                # §V5: a factual result carries region provenance; en is not mixed.
                provenance = envelope["provenance"]
                assert provenance and provenance[0]["server"] == "en"


def test_authenticated_remote_wire_serves_shared_core(
    remote_server: tuple[str, LocalOidcIssuer],
) -> None:
    # §V14: a validly-authenticated Streamable HTTP client is served the identical
    # shared core stdio serves -- same serverInfo/instructions/tool set, and a typed
    # ok envelope with en provenance -- proving the remote transport reuses the one
    # registry + services rather than a divergent remote path.
    url, issuer = remote_server
    anyio.run(_drive_authenticated, url, issuer.mint())


def test_remote_wire_refuses_missing_bearer(
    remote_server: tuple[str, LocalOidcIssuer],
) -> None:
    # §V10/§V40: auth is genuinely enforced on the wire -- a request without a bearer
    # is refused with a typed 401 challenge before it ever reaches the session
    # manager, and the response leaks no token (there is none) or internal detail.
    url, _issuer = remote_server
    resp = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}, timeout=30)
    assert resp.status_code == 401
    challenge = resp.headers.get("www-authenticate", "")
    assert 'error="invalid_token"' in challenge
    body = resp.text
    assert "Traceback" not in body
    assert str(REPO_ROOT) not in body
