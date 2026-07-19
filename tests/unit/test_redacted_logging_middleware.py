"""§T54/§V12: the remote access log carries no secret.

Drives :class:`~arknights_mcp.middleware.logging.RedactedLoggingMiddleware` with a
raw ASGI ``(scope, receive, send)`` and captures the emitted log records, asserting
the access line records method / path / status / principal id -- and never the
bearer token, the ``Authorization`` header, the request body, or the response body
(§V12). The guarantee is by construction: the middleware never reads any of those.
"""

from __future__ import annotations

import logging
from typing import Any

import anyio
import pytest

from arknights_mcp.auth.principal import Principal
from arknights_mcp.middleware.logging import RedactedLoggingMiddleware

_PRINCIPAL = Principal(
    issuer="https://issuer.example.com/",
    subject="auth0|alice",
    client_id="client-a",
    scopes=frozenset({"arknights:read"}),
)

_SECRET_TOKEN = "eyJhbGciOiJSUzI1NiJ9.super.secret.jwt"  # noqa: S105 (test literal)
_REQUEST_BODY = b'{"jsonrpc":"2.0","method":"tools/call","params":{"secret":"body"}}'
_RESPONSE_BODY = b'{"result":"sensitive response payload"}'


class _InnerApp:
    """Attaches the validated principal (as the bearer layer would) and replies."""

    def __init__(self, *, attach_principal: bool = True) -> None:
        self.attach_principal = attach_principal

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # Drain the (secret-carrying) request body, as the real handler would.
        await receive()
        if self.attach_principal:
            scope["state"] = {**scope.get("state", {}), "principal": _PRINCIPAL}
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": _RESPONSE_BODY})


def _drive(app: RedactedLoggingMiddleware, *, scope_type: str = "http") -> None:
    scope: dict[str, Any] = {"type": scope_type, "method": "POST", "path": "/mcp"}
    if scope_type == "http":
        scope["headers"] = [
            (b"authorization", f"Bearer {_SECRET_TOKEN}".encode("latin-1")),
            (b"content-length", str(len(_REQUEST_BODY)).encode("ascii")),
        ]

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": _REQUEST_BODY, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        pass

    anyio.run(app.__call__, scope, receive, send)


def test_access_line_records_method_path_status_principal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = RedactedLoggingMiddleware(_InnerApp())
    with caplog.at_level(logging.INFO, logger="arknights_mcp.access"):
        _drive(app)
    records = [r for r in caplog.records if r.name == "arknights_mcp.access"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "POST" in message
    assert "/mcp" in message
    assert "200" in message
    # The principal id (iss|sub) is recorded -- the identity, not the credential.
    assert _PRINCIPAL.principal_id in message


def test_access_line_never_leaks_token_header_or_bodies(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # §V12: the emitted record must contain neither the bearer token, the raw
    # Authorization header value, the request body, nor the response body.
    app = RedactedLoggingMiddleware(_InnerApp())
    with caplog.at_level(logging.INFO, logger="arknights_mcp.access"):
        _drive(app)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert _SECRET_TOKEN not in blob
    assert "Bearer" not in blob
    assert "secret" not in blob  # from the request body
    assert "sensitive response payload" not in blob  # from the response body


def test_pre_auth_request_logged_as_anonymous(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A request rejected before a principal is attached still gets an access line,
    # bucketed as anonymous -- the log records the outcome without inventing an id.
    app = RedactedLoggingMiddleware(_InnerApp(attach_principal=False))
    with caplog.at_level(logging.INFO, logger="arknights_mcp.access"):
        _drive(app)
    records = [r for r in caplog.records if r.name == "arknights_mcp.access"]
    assert len(records) == 1
    assert "principal=anonymous" in records[0].getMessage()


def test_non_http_scope_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    app = RedactedLoggingMiddleware(_InnerApp())
    with caplog.at_level(logging.INFO, logger="arknights_mcp.access"):

        async def receive() -> dict[str, Any]:
            return {"type": "lifespan.startup"}

        async def send(message: dict[str, Any]) -> None:
            pass

        anyio.run(app.__call__, {"type": "lifespan"}, receive, send)
    assert [r for r in caplog.records if r.name == "arknights_mcp.access"] == []
