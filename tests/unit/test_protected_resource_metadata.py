"""§T81/§V45: unauthenticated RFC 9728 OAuth discovery in the Streamable HTTP stack.

An MCP OAuth client (`claude mcp login`) bootstraps auth by fetching the
protected-resource metadata -- a request that cannot itself carry a bearer. This
suite drives the discovery layer with a raw ASGI ``(scope, receive, send)`` (no
socket, no uvicorn) and asserts:

* the metadata document is served ``200`` *without* a bearer at both well-known paths
  (bare root + resource-path suffix) and carries the RFC 9728 fields (issuer only, no
  secret);
* every other path -- including ``/mcp`` -- falls through to the wrapped app, so the
  bearer gate still stands;
* the composed :func:`wrap_remote_app` stack serves discovery unauthenticated while
  ``/mcp`` still yields a ``401`` whose challenge carries the ``resource_metadata``
  hint (RFC 9728 §5.1).
"""

from __future__ import annotations

import json
from typing import Any

import anyio

from arknights_mcp.auth.oidc import OidcSettings
from arknights_mcp.auth.principal import Principal
from arknights_mcp.config import AppConfig
from arknights_mcp.transports.streamable_http import (
    _BearerAuthASGIApp,
    _prm_paths,
    _prm_url,
    _protected_resource_metadata,
    _ProtectedResourceMetadataASGIApp,
    wrap_remote_app,
)

_SETTINGS = OidcSettings(
    issuer="https://dev-tenant.us.auth0.com/",
    audience="https://arknights-mcp.example.com/mcp",
    jwks_url="https://dev-tenant.us.auth0.com/.well-known/jwks.json",
    required_scopes=("arknights:read",),
)


class _InnerApp:
    """Records whether it ran; a stand-in for the bearer-gated app below discovery."""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner"})


def _drive(
    app: Any, *, path: str, method: str = "GET", headers: list[tuple[bytes, bytes]] | None = None
) -> list[dict[str, Any]]:
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
    }
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    anyio.run(app.__call__, scope, receive, send)
    return sent


def _status(sent: list[dict[str, Any]]) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _headers(sent: list[dict[str, Any]]) -> dict[bytes, bytes]:
    start = next(m for m in sent if m["type"] == "http.response.start")
    return dict(start["headers"])


def _body(sent: list[dict[str, Any]]) -> bytes:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def _www_authenticate(sent: list[dict[str, Any]]) -> str:
    return _headers(sent).get(b"www-authenticate", b"").decode("latin-1")


# --- the metadata document builder (§V45) -----------------------------------------


def test_metadata_document_shape() -> None:
    remote = AppConfig().mcp.remote  # public_base_url default https://, path /mcp
    doc = _protected_resource_metadata(remote, _SETTINGS)
    assert doc["resource"] == remote.public_base_url.rstrip("/") + remote.path
    # Advertises the issuer ONLY -- the client fetches AS metadata from it (§V1).
    assert doc["authorization_servers"] == ["https://dev-tenant.us.auth0.com/"]
    assert doc["scopes_supported"] == ["arknights:read"]
    assert doc["bearer_methods_supported"] == ["header"]
    # §V12: no secret ever appears in the discovery document.
    blob = json.dumps(doc)
    assert _SETTINGS.jwks_url not in blob  # jwks url is not advertised here
    assert "oauth-authorization-server" not in blob  # AS metadata never served/proxied


def test_prm_paths_cover_root_and_suffix() -> None:
    assert _prm_paths("/mcp") == {
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    }


def test_prm_url_is_absolute_suffixed() -> None:
    remote = AppConfig().mcp.remote
    assert _prm_url(remote) == (
        remote.public_base_url.rstrip("/") + "/.well-known/oauth-protected-resource/mcp"
    )


# --- the unauthenticated discovery layer (§V45) -----------------------------------


def _metadata_app(inner: _InnerApp) -> _ProtectedResourceMetadataASGIApp:
    remote = AppConfig().mcp.remote
    return _ProtectedResourceMetadataASGIApp(
        inner,
        paths=_prm_paths(remote.path),
        document=_protected_resource_metadata(remote, _SETTINGS),
    )


def test_discovery_served_without_bearer_at_root() -> None:
    inner = _InnerApp()
    app = _metadata_app(inner)
    sent = _drive(app, path="/.well-known/oauth-protected-resource")
    assert _status(sent) == 200
    assert inner.called is False  # answered above the wrapped (bearer-gated) app
    doc = json.loads(_body(sent))
    assert doc["authorization_servers"] == ["https://dev-tenant.us.auth0.com/"]
    assert _headers(sent)[b"content-type"] == b"application/json"


def test_discovery_served_at_resource_suffix_path() -> None:
    inner = _InnerApp()
    sent = _drive(_metadata_app(inner), path="/.well-known/oauth-protected-resource/mcp")
    assert _status(sent) == 200
    assert inner.called is False


def test_discovery_head_has_empty_body() -> None:
    inner = _InnerApp()
    sent = _drive(_metadata_app(inner), path="/.well-known/oauth-protected-resource", method="HEAD")
    assert _status(sent) == 200
    assert _body(sent) == b""


def test_mcp_path_falls_through_to_inner() -> None:
    # /mcp is NOT a discovery path: it must reach the wrapped (bearer-gated) app.
    inner = _InnerApp()
    sent = _drive(_metadata_app(inner), path="/mcp", method="POST")
    assert inner.called is True
    assert _body(sent) == b"inner"


def test_unrelated_wellknown_falls_through() -> None:
    inner = _InnerApp()
    _drive(_metadata_app(inner), path="/.well-known/openid-configuration")
    assert inner.called is True  # we neither serve nor proxy AS metadata (§V1)


# --- the composed stack: discovery open, /mcp still gated (§V45) -------------------


class _AcceptAllVerifier:
    """Verifier stub; unreached here -- a no-bearer /mcp is rejected before verify."""

    def verify(self, token: str) -> Principal:  # pragma: no cover - not reached in these tests
        raise AssertionError("unexpected token verification")


def _wrapped_stack() -> Any:
    # wrap_remote_app always composes the auth stack; a no-bearer /mcp is refused at
    # the bearer layer before the verifier runs, so the stub is never invoked.
    return wrap_remote_app(_InnerApp(), AppConfig(), _AcceptAllVerifier(), _SETTINGS)  # type: ignore[arg-type]


def test_stack_serves_discovery_but_gates_mcp() -> None:
    app = _wrapped_stack()

    # Discovery: reachable with no bearer.
    disc = _drive(app, path="/.well-known/oauth-protected-resource")
    assert _status(disc) == 200
    doc = json.loads(_body(disc))
    assert doc["authorization_servers"] == ["https://dev-tenant.us.auth0.com/"]

    # /mcp with no bearer: still refused, and the challenge points at discovery.
    gated = _drive(app, path="/mcp", method="POST")
    assert _status(gated) == 401
    challenge = _www_authenticate(gated)
    assert 'error="invalid_token"' in challenge
    assert "resource_metadata=" in challenge
    assert "/.well-known/oauth-protected-resource/mcp" in challenge


def test_bearer_challenge_carries_resource_metadata() -> None:
    # §V45 / RFC 9728 §5.1: the resource_metadata hint is emitted on the challenge.
    inner = _InnerApp()
    app = _BearerAuthASGIApp(
        inner,
        _AcceptAllVerifier(),  # type: ignore[arg-type]
        _SETTINGS,
        resource_metadata_url="https://arknights-mcp.example.com/.well-known/oauth-protected-resource/mcp",
    )
    sent = _drive(app, path="/mcp", method="POST")
    assert _status(sent) == 401
    assert (
        'resource_metadata="https://arknights-mcp.example.com/'
        '.well-known/oauth-protected-resource/mcp"' in _www_authenticate(sent)
    )
    assert inner.called is False
