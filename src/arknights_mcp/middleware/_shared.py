"""Shared ASGI helpers for the remote middleware stack (§V37).

One home for the request-scope principal lookup and the typed JSON error response
used by the per-principal rate limiter and the per-request size/timeout limiter --
neither re-implements them (§V37 DRY). The bearer challenge in
:mod:`arknights_mcp.transports.streamable_http` stays separate: it emits an RFC 6750
``WWW-Authenticate`` challenge, a different concern from these limiter rejections.
"""

from __future__ import annotations

import json

from starlette.types import Message, Scope, Send

#: Bucket key for a request that carries no validated principal. The full stack is
#: wired only on the auth-requiring remote path (§V40), so in practice every http
#: request reaching the per-principal limiters already carries a principal; this is
#: the defensive fallback (and the identity the outer access log records for a
#: request rejected by bearer before any principal was attached).
ANONYMOUS_PRINCIPAL = "anonymous"


def principal_id_of(scope: Scope) -> str:
    """Return the validated principal's ``iss|sub`` id, else ``ANONYMOUS_PRINCIPAL``.

    The bearer middleware stashes the validated
    :class:`~arknights_mcp.auth.principal.Principal` on ``scope["state"]["principal"]``
    after §V10 validation; per-principal limits (§V11) key on its ``principal_id``
    (``iss|sub`` -- the one home for the namespacing, §V37). Read defensively
    (duck-typed) so the middleware layer takes no dependency on the auth layer.
    """
    state = scope.get("state") or {}
    principal = state.get("principal")
    principal_id = getattr(principal, "principal_id", None)
    return principal_id if isinstance(principal_id, str) else ANONYMOUS_PRINCIPAL


async def send_error(
    send: Send,
    status: int,
    error: str,
    description: str,
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    """Emit a typed JSON error response carrying no secrets (§V12).

    The body holds only the static OAuth-style ``error`` / ``error_description`` --
    never the token, a request/response body, or any presented header value. Used
    for the limiter rejections (413 / 429 / 504); ``extra_headers`` carries e.g. a
    ``Retry-After`` hint.
    """
    body = json.dumps({"error": error, "error_description": description}).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/json")]
    if extra_headers:
        headers.extend(extra_headers)
    start: Message = {"type": "http.response.start", "status": status, "headers": headers}
    await send(start)
    await send({"type": "http.response.body", "body": body})
