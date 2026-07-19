"""Redacted access logging for the remote transport (§V12).

Emits one operational access line per ``http`` request:

    <method> <path> -> <status> (<dur_ms>ms) principal=<iss|sub>

and nothing else. §V12 forbids default logs from carrying the full prompt, full
tool arguments, a response body, the ``Authorization`` header, a bearer token, a raw
source record, or roster/account data. This middleware satisfies that *by
construction*: it reads only the request ``method``/``path``, the response
``status``, and the validated principal id already on the scope -- it never reads the
request body, the response body, or any request header, so there is no code path by
which a secret could reach the log.

The two :class:`~arknights_mcp.config.PrivacyConfig` opt-ins
(``log_tool_arguments`` / ``log_tool_results``) are deliberately *not* honored here:
this is the wire-level access log, which has neither the tool arguments nor the tool
results in hand (they live inside the opaque MCP request/response bodies this layer
never parses). Those flags govern the tool-dispatch layer, if ever enabled; the
access log stays body-blind regardless, so flipping them on can never make this log
leak (fail-closed, §V12).

Placed as the OUTERMOST layer of the remote stack so it records every request's
outcome -- including a ``401`` from the bearer challenge or a ``429`` from the rate
limiter -- with the status the client actually received. For a request rejected
before authentication the principal is unknown, logged as ``anonymous``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from time import monotonic

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from arknights_mcp.middleware._shared import principal_id_of

_LOG = logging.getLogger("arknights_mcp.access")


class RedactedLoggingMiddleware:
    """ASGI middleware emitting a body-blind access log per request (§V12).

    :param app: the wrapped ASGI app (the next layer inward).
    :param clock: monotonic time source (injectable for deterministic tests).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._app = app
        self._clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method = scope.get("method", "-")
        path = scope.get("path", "-")
        started = self._clock()
        status = 0

        async def logging_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self._app(scope, receive, logging_send)
        finally:
            # Read the principal *after* the inner stack ran: the bearer layer, if it
            # validated, has by then stashed it on the scope. Only the id (iss|sub) is
            # logged -- never the token that produced it (§V12).
            principal_id = principal_id_of(scope)
            duration_ms = (self._clock() - started) * 1000.0
            _LOG.info(
                "%s %s -> %d (%.1fms) principal=%s",
                method,
                path,
                status,
                duration_ms,
                principal_id,
            )
