"""§T57 remote security/privacy matrix over a real loopback socket (§V10/§V11/§V12).

The adversarial half of the remote validation §T56 deferred: it drives the *full*
auth-requiring remote stack (redacted logging → bearer → rate/concurrency → request
limits → session manager) over uvicorn, exactly as a remote MCP host would, but with
*attack-shaped* traffic rather than an honest bearer. The unit suite already proves
each layer in isolation (``test_oidc_validation`` the real verifier with no stack;
``test_bearer_auth_asgi`` / ``test_remote_middleware_stack`` the composed stack with a
*stubbed* verifier; ``test_session_isolation``; ``test_redacted_logging_middleware``).
This suite closes the remaining gap: **real attack tokens through the composed stack,
on the wire, validated by the real** :class:`~arknights_mcp.auth.oidc.OidcTokenVerifier`
(only the JWKS key fetch is local -- :class:`~tests.support.oidc_issuer.LocalOidcIssuer`).

Coverage vs the §T57 line:

* **token missing / expired / wrong-issuer / wrong-aud / insufficient-scope** (+ a
  non-``Bearer`` scheme, ``alg=none``, and a foreign-key signature for §V10 depth):
  each is refused with the typed ``401 invalid_token`` / ``403 insufficient_scope``
  ``WWW-Authenticate`` challenge, and the response leaks neither the presented token,
  a JWT segment, a stack trace, nor a local path (§V10/§V12/§V23);
* **isolation**: a session established by one principal cannot be resumed by another
  over the wire -- the cross-principal probe is refused ``404`` and the owner's
  session stays live (§V10/§V14/§T53);
* **rate limit**: a per-principal cap is enforced on the wire -- the over-cap request
  is refused ``429`` with a ``Retry-After`` and no leak (§V11);
* **log scan**: under real authenticated *and* rejected traffic the
  ``arknights_mcp.access`` log records only method/path/status/principal -- never the
  bearer, the ``Authorization`` header, or a tool argument (§V12).

Offline + deterministic: build promoted from the pinned 4-4 fixture via the real
``import`` path (no network, §V1); OIDC keypair + JWKS local (no provider reached).
The shared uvicorn + fixture-import scaffolding has one home in
:mod:`tests.support.remote_harness` (§V37).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import anyio
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from tests.support.oidc_issuer import LocalOidcIssuer
from tests.support.remote_harness import EXPECTED_TOOLS, REPO_ROOT, remote_server

_ACCESS_LOGGER = "arknights_mcp.access"

#: A private RSA key the issuer's JWKS resolver does NOT serve -- a token signed with
#: it fails the §V10 signature check even though its claims are otherwise valid.
_FOREIGN_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

#: A minimal JSON-RPC body. Every attack request is refused at the bearer layer
#: *before* the session manager, so the body is never parsed -- it only has to be
#: well-formed JSON for httpx to send.
_PROBE_BODY = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}


@pytest.fixture(scope="module")
def secured_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, LocalOidcIssuer]]:
    """One authenticated remote server for the whole matrix (default limits).

    Module-scoped: the auth-rejection cases, isolation, and log scan all share it (a
    rejected request never reaches the rate limiter, and the honest requests are few,
    so the default 60/min cap is never a factor). The §V11 rate-limit test builds its
    own low-cap server.
    """
    tmp = tmp_path_factory.mktemp("t57-remote")
    with remote_server(tmp) as served:
        yield served


def _claims(issuer: LocalOidcIssuer, **overrides: object) -> dict[str, object]:
    """Build otherwise-valid claims for this issuer, for hand-signed attack tokens."""
    settings = issuer.settings
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": settings.issuer,
        "sub": "auth0|remote-tester",
        "aud": settings.audience,
        "exp": now + 3600,
        "iat": now,
        "azp": "client-x",
        "scope": " ".join(settings.required_scopes),
    }
    claims.update(overrides)
    return claims


@dataclass(frozen=True)
class _Case:
    """One adversarial-token row: how to build its ``Authorization``, and the reject."""

    name: str
    make_header: Callable[[LocalOidcIssuer], str | None]
    status: int
    error: str
    scope_hint: bool = False


_MATRIX: tuple[_Case, ...] = (
    _Case("missing", lambda i: None, 401, "invalid_token"),
    _Case("non_bearer_scheme", lambda i: "Basic dXNlcjpwYXNz", 401, "invalid_token"),
    _Case(
        "expired",
        lambda i: "Bearer " + i.mint(exp=int(time.time()) - 120, iat=int(time.time()) - 3600),
        401,
        "invalid_token",
    ),
    _Case(
        "wrong_issuer",
        lambda i: "Bearer " + i.mint(iss="https://evil.example.com/"),
        401,
        "invalid_token",
    ),
    _Case(
        "wrong_audience",
        lambda i: "Bearer " + i.mint(aud="some-other-api"),
        401,
        "invalid_token",
    ),
    _Case(
        "alg_none",
        lambda i: "Bearer " + jwt.encode(_claims(i), key="", algorithm="none"),
        401,
        "invalid_token",
    ),
    _Case(
        "foreign_signature",
        lambda i: "Bearer " + jwt.encode(_claims(i), _FOREIGN_KEY, algorithm="RS256"),
        401,
        "invalid_token",
    ),
    _Case(
        "insufficient_scope",
        lambda i: "Bearer " + i.mint(scope="other:read"),
        403,
        "insufficient_scope",
        scope_hint=True,
    ),
)


@pytest.mark.parametrize("case", _MATRIX, ids=lambda c: c.name)
def test_attack_token_refused_without_leak(
    secured_server: tuple[str, LocalOidcIssuer], case: _Case
) -> None:
    # §V10: every attack-shaped credential is refused by the real verifier over the
    # wire with the typed challenge, before it reaches the session manager; §V12/§V23:
    # the response leaks neither the presented token, a JWT segment, a stack trace,
    # nor a local path.
    url, issuer = secured_server
    header = case.make_header(issuer)
    headers = {"Authorization": header} if header is not None else {}
    resp = httpx.post(url, json=_PROBE_BODY, headers=headers, timeout=30)

    assert resp.status_code == case.status
    challenge = resp.headers.get("www-authenticate", "")
    assert f'error="{case.error}"' in challenge
    if case.scope_hint:
        assert 'scope="arknights:read"' in challenge

    body = resp.text
    assert "Traceback" not in body
    assert str(REPO_ROOT) not in body
    # No JWT (header.payload.signature -> starts "eyJ") ever appears in the reply.
    assert "eyJ" not in body
    assert "eyJ" not in challenge
    if header is not None and header.startswith("Bearer "):
        presented = header[len("Bearer ") :]
        assert presented not in body
        assert presented not in challenge


def _init_and_get_session_id(url: str, token: str) -> str:
    """Establish an authenticated MCP session; return its ``Mcp-Session-Id``."""

    captured: dict[str, str] = {}

    async def _run() -> None:
        http_client = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
        with anyio.fail_after(60):
            async with (
                http_client,
                streamable_http_client(url, http_client=http_client) as (
                    read_stream,
                    write_stream,
                    get_session_id,
                ),
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    sid = get_session_id()
                    assert sid, "server issued no Mcp-Session-Id"
                    captured["sid"] = sid

    anyio.run(_run)
    return captured["sid"]


def test_cross_principal_cannot_resume_session(
    secured_server: tuple[str, LocalOidcIssuer],
) -> None:
    # §V10/§V14/§T53: Alice establishes a session over the authenticated wire; Bob (a
    # different principal, same issuer) presents Alice's session id with his own valid
    # bearer and is refused 404 -- the SDK owner-binding, fed our principal-keyed user,
    # rejects the cross-user resume before dispatch, and leaks nothing.
    url, issuer = secured_server
    alice = issuer.mint(sub="auth0|alice")
    bob = issuer.mint(sub="auth0|bob")

    session_id = _init_and_get_session_id(url, alice)

    resp = httpx.post(
        url,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers={
            "Authorization": f"Bearer {bob}",
            "Mcp-Session-Id": session_id,
            "Accept": "application/json, text/event-stream",
        },
        timeout=30,
    )
    assert resp.status_code == 404
    body = resp.text
    assert "Traceback" not in body
    assert str(REPO_ROOT) not in body
    assert bob not in body


def _drive_tool_call(url: str, token: str, *, game_id: str) -> None:
    """Authenticated initialize + one ``get_enemy`` call over the wire."""

    async def _run() -> None:
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
                    await session.initialize()
                    listed = await session.list_tools()
                    assert {t.name for t in listed.tools} == EXPECTED_TOOLS
                    result = await session.call_tool(
                        "get_enemy", {"server": "en", "game_id": game_id}
                    )
                    assert result.isError is False

    anyio.run(_run)


def _access_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _ACCESS_LOGGER]


def _wait_for_access_records(
    caplog: pytest.LogCaptureFixture, minimum: int, *, timeout: float = 5.0
) -> list[logging.LogRecord]:
    # The access line is emitted in the outermost middleware's ``finally``, which can
    # run on the server thread just after the client has the response -- poll briefly.
    deadline = time.time() + timeout
    while time.time() < deadline:
        records = _access_records(caplog)
        if len(records) >= minimum:
            return records
        time.sleep(0.05)
    return _access_records(caplog)


def test_access_log_scrubbed_under_real_traffic(
    secured_server: tuple[str, LocalOidcIssuer],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # §V12: under a real authenticated tool call AND a rejected request, the access
    # log records only method/path/status/principal -- never the bearer, the raw
    # Authorization header, or a tool argument (the game_id). A rejected request is
    # still recorded (logging is outermost) as anonymous.
    url, issuer = secured_server
    token = issuer.mint()
    game_id = "enemy_1007_slime"

    with caplog.at_level(logging.INFO, logger=_ACCESS_LOGGER):
        _drive_tool_call(url, token, game_id=game_id)
        rejected = httpx.post(
            url,
            json=_PROBE_BODY,
            headers={"Authorization": "Bearer bad.attacker.jwt"},
            timeout=30,
        )
        assert rejected.status_code == 401
        records = _wait_for_access_records(caplog, minimum=2)

    blob = "\n".join(r.getMessage() for r in records)
    # Identity is recorded (iss|sub), the outcome is recorded ...
    assert "auth0|remote-tester" in blob
    assert "principal=anonymous" in blob
    # ... but no credential, header, or tool argument ever reaches the log.
    assert token not in blob
    assert "bad.attacker.jwt" not in blob
    assert "Bearer" not in blob
    assert "Authorization" not in blob
    assert game_id not in blob


def test_rate_limit_enforced_over_wire(tmp_path: Path) -> None:
    # §V11: a per-principal rate cap is enforced on the wire. With a 2/min cap the
    # third authenticated request is refused 429 with a Retry-After hint, and the
    # rejection leaks no token. (Auth passes; the limiter counts at reserve, before
    # the inner app, so the count holds regardless of each request's inner outcome.)
    with remote_server(tmp_path, limits={"requests_per_minute_per_principal": 2}) as (url, issuer):
        token = issuer.mint()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        first = httpx.post(url, json=body, headers=headers, timeout=30)
        second = httpx.post(url, json=body, headers=headers, timeout=30)
        third = httpx.post(url, json=body, headers=headers, timeout=30)

    # The first two pass the limiter (whatever the manager then makes of a
    # session-less tools/list); the third crosses the cap.
    assert first.status_code != 429
    assert second.status_code != 429
    assert third.status_code == 429
    assert third.headers.get("retry-after")
    assert token not in third.text
    assert "Traceback" not in third.text
