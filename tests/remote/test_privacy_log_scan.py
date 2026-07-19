"""§T62/§V12: end-to-end privacy scan of the whole logging surface.

Where the §T57 log-scan (:mod:`tests.remote.test_remote_security_privacy`
``test_access_log_scrubbed_under_real_traffic``) asserts the *single*
``arknights_mcp.access`` logger stays scrubbed, this M7 scan is broader: it drives
the full auth-requiring remote stack over a real loopback socket with
representative traffic -- a valid bearer, a large tool-argument blob, a request
whose response carries real fact-body content, and a rejected request presenting a
sentinel token -- while capturing **every** record from **every** logger in the
process (root + force-``propagate`` named loggers at the default operational INFO
level), then asserts none of the §V12 forbidden items surface anywhere.

§V12 forbids default logs from carrying: the full prompt, the full tool arguments,
a response body, the ``Authorization`` header, a bearer token, a raw source record,
or roster/account data. This scan attaches a deterministic sentinel to each
injectable item and asserts its absence across the captured blob:

* **bearer token / ``Authorization`` header** -- the minted JWT, its ``eyJ`` prefix,
  the literal ``Bearer`` scheme + ``Authorization`` header name, and a distinct
  sentinel token on a *rejected* request (the pre-auth path);
* **full prompt / full tool args** -- a 114-char sentinel ``game_id`` (within the
  §V18 cap) that travels in the JSON-RPC request body of a ``get_enemy`` call;
* **response body** -- a real fact value (``Originium Slug``) + the resolved
  ``game_id`` returned by an ``ok`` ``get_enemy`` call;
* **raw source record** -- the raw upstream shape keys (``enemyData`` / ``m_value``)
  that only exist in the pre-normalization snapshot, never a query-time read.

Roster/account is not a concept v0.1 handles -- §V15 forbids storing game
credentials at all -- so the only identity that reaches the log is the OAuth
principal id ``iss|sub``, logged by design; it is asserted *present* as a positive
control, not scanned for absence.

Capture is at the default operational **INFO** level, not DEBUG: §V12 governs
*default* logs, so this scans the surface a production operator actually runs, not
opt-in developer message tracing. Offline + deterministic: build promoted from the
pinned 4-4 fixture (no network, §V1); OIDC keypair + JWKS local. The uvicorn +
fixture-import scaffolding is reused from :mod:`tests.support.remote_harness`
(§V37) -- this file adds only the whole-surface capture + sentinel assertions.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

import anyio
import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from tests.support.oidc_issuer import LocalOidcIssuer
from tests.support.remote_harness import EXPECTED_TOOLS, remote_server

_ACCESS_LOGGER = "arknights_mcp.access"

#: A minimal JSON-RPC body for the rejected request; refused at the bearer layer
#: before the session manager, so it is never parsed -- it only has to be well-formed.
_PROBE_BODY = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}

#: A real fact value the pinned 4-4 fixture surfaces via ``get_enemy`` -- present in
#: the ``ok`` response body, and thus a §V12 response-body sentinel to scan for.
_FACT_BODY_VALUE = "Originium Slug"
#: A real enemy game_id in the fixture (both the ``ok`` request arg and its body).
_REAL_GAME_ID = "enemy_1007_slime"

#: A large-but-valid (§V18 ``MAX_ID_LEN`` = 128) sentinel game_id: it flows in the
#: JSON-RPC request body of a ``get_enemy`` call (the "full prompt" / "full tool
#: args" surface) yet matches nothing, so the call is a clean ``not_found``.
_LARGE_ARG_SENTINEL = "PRIVACYSCANARGSENTINEL" + "A" * 92

#: A distinct sentinel bearer on a rejected request -- must not surface even on the
#: pre-auth failure path (logged ``anonymous``, but the presented token stays out).
_REJECTED_TOKEN_SENTINEL = "SENTINELBADTOKENdeadbeef"  # noqa: S105 (test sentinel)


@pytest.fixture(scope="module")
def secured_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, LocalOidcIssuer]]:
    """One authenticated remote server for the scan (default limits; §V37 harness)."""
    tmp = tmp_path_factory.mktemp("t62-privacy-scan")
    with remote_server(tmp) as served:
        yield served


class _CapturingHandler(logging.Handler):
    """Accumulates every :class:`logging.LogRecord` routed to it (§V12 scan sink)."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _capture_every_logger() -> Iterator[_CapturingHandler]:
    """Capture every record from every logger in the process (§V12 whole-surface).

    Adds an INFO handler to the root logger and forces every *existing* named logger
    to INFO + ``propagate=True`` so a record from any layer (the app, the MCP SDK,
    uvicorn, the auth backend) reaches the capture regardless of its own handler /
    propagation config. INFO (not DEBUG) is deliberate: §V12 governs *default* logs,
    the operational surface a production run emits -- not opt-in developer tracing.
    Original levels/propagation are restored on exit.
    """
    root = logging.getLogger()
    handler = _CapturingHandler()
    saved_root_level = root.level
    saved: list[tuple[logging.Logger, int, bool]] = []
    for obj in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(obj, logging.Logger):
            saved.append((obj, obj.level, obj.propagate))
            obj.setLevel(logging.INFO)
            obj.propagate = True
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(saved_root_level)
        for logger, level, propagate in saved:
            logger.setLevel(level)
            logger.propagate = propagate


def _blob(handler: _CapturingHandler) -> str:
    """Render every captured record (name + level + rendered message + any trace)."""
    fmt = logging.Formatter("%(name)s %(levelname)s %(message)s")
    return "\n".join(fmt.format(record) for record in handler.records)


def _wait_for_access_lines(
    handler: _CapturingHandler, minimum: int, *, timeout: float = 5.0
) -> None:
    """Poll until >= ``minimum`` access lines are captured (emitted on the server thread).

    The outermost middleware emits its access line in a ``finally`` that can run on
    the uvicorn thread just after the client already holds the response, so the scan
    must wait for those records to land before reading the blob.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sum(1 for r in handler.records if r.name == _ACCESS_LOGGER) >= minimum:
            return
        time.sleep(0.05)


def _drive_authenticated_flow(url: str, token: str) -> None:
    """Initialize + list_tools + a large-arg ``not_found`` + a real ``ok`` get_enemy.

    Exercises the representative request flow the scan needs in one authenticated
    session: a large tool-argument blob in a request body, and a call whose response
    body carries real fact content.
    """

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
                    # Large tool-arg blob in the request body -> clean not_found.
                    big = await session.call_tool(
                        "get_enemy", {"server": "en", "game_id": _LARGE_ARG_SENTINEL}
                    )
                    assert big.isError is False
                    # A real lookup whose ok response body carries fact content.
                    hit = await session.call_tool(
                        "get_enemy", {"server": "en", "game_id": _REAL_GAME_ID}
                    )
                    assert hit.isError is False

    anyio.run(_run)


def test_no_forbidden_item_in_any_log(
    secured_server: tuple[str, LocalOidcIssuer],
) -> None:
    # §V12: drive the full remote stack with a valid bearer, a large tool-arg blob,
    # a body-bearing response, and a rejected sentinel-token request; capture EVERY
    # logger's output and assert no forbidden item surfaces anywhere.
    url, issuer = secured_server
    token = issuer.mint()

    with _capture_every_logger() as handler:
        _drive_authenticated_flow(url, token)
        rejected = httpx.post(
            url,
            json=_PROBE_BODY,
            headers={"Authorization": f"Bearer {_REJECTED_TOKEN_SENTINEL}"},
            timeout=30,
        )
        assert rejected.status_code == 401
        # Wait for the authenticated flow's + the rejected request's access lines.
        _wait_for_access_lines(handler, minimum=2)

    blob = _blob(handler)

    # Positive controls: logging DID happen at the scanned level, and the OAuth
    # principal id (iss|sub) is recorded by design -- so the absences below are
    # meaningful, not an artifact of an empty capture.
    assert _ACCESS_LOGGER in blob
    assert "auth0|remote-tester" in blob  # principal id (iss|sub), logged by design
    assert "principal=anonymous" in blob  # the rejected pre-auth request

    # §V12 forbidden items -- each a deterministic sentinel that must NOT appear.
    forbidden = {
        "bearer token": token,
        "bearer JWT prefix": "eyJ",
        "authorization scheme": "Bearer",
        "authorization header name": "Authorization",
        "rejected-request bearer token": _REJECTED_TOKEN_SENTINEL,
        "full tool args / request body (game_id)": _LARGE_ARG_SENTINEL,
        "response body (fact value)": _FACT_BODY_VALUE,
        "response body (resolved game_id)": _REAL_GAME_ID,
        "raw source record (enemyData)": "enemyData",
        "raw source record (m_value)": "m_value",
    }
    leaked = {label: needle for label, needle in forbidden.items() if needle in blob}
    assert not leaked, f"§V12 leak in captured logs: {sorted(leaked)}"
