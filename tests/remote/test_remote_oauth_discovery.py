"""§T81/§V45: RFC 9728 OAuth discovery over the real loopback wire.

The unit suite (`tests/unit/test_protected_resource_metadata.py`) proves the discovery
layer in isolation with a raw ASGI drive. This closes the on-the-wire gap the memo
called out: through the *full* auth-requiring remote stack served by uvicorn -- exactly
as `claude mcp login` would hit it -- the protected-resource metadata is reachable
**without a bearer**, while ``/mcp`` still refuses an unauthenticated request ``401``
and points the client at that metadata (RFC 9728 §5.1). Offline: build + JWKS local
(no network, §V1/§V10), shared scaffolding in :mod:`tests.support.remote_harness`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import jwt
import pytest
from tests.support.oidc_issuer import LocalOidcIssuer
from tests.support.remote_harness import remote_server

_WELL_KNOWN = "/.well-known/oauth-protected-resource"


@pytest.fixture(scope="module")
def secured_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, LocalOidcIssuer]]:
    tmp = tmp_path_factory.mktemp("t81-discovery")
    with remote_server(tmp) as served:
        yield served


def _origin(mcp_url: str) -> str:
    """Strip the ``/mcp`` suffix to get the server origin the well-known hangs off."""
    assert mcp_url.endswith("/mcp")
    return mcp_url[: -len("/mcp")]


def test_protected_resource_metadata_served_without_bearer(
    secured_server: tuple[str, LocalOidcIssuer],
) -> None:
    # §V45: the RFC 9728 metadata is reachable with NO Authorization header, at both
    # the bare well-known root and the resource-path-suffixed form.
    url, issuer = secured_server
    origin = _origin(url)
    for path in (_WELL_KNOWN, _WELL_KNOWN + "/mcp"):
        resp = httpx.get(origin + path, timeout=30)
        assert resp.status_code == 200, path
        assert resp.headers["content-type"].startswith("application/json")
        doc = resp.json()
        # Advertises the issuer ONLY -- the client fetches AS metadata from it (§V1).
        assert doc["authorization_servers"] == [issuer.settings.issuer]
        # §V45 scope split (B126): the ADVERTISE list on the wire -- a superset of the
        # required scopes, carrying the flow-only `offline_access` so an interactive
        # client asks for a refresh token instead of a bearer dead in ~24h.
        assert doc["scopes_supported"] == list(issuer.settings.advertised_scopes)
        assert set(issuer.settings.required_scopes) <= set(doc["scopes_supported"])
        assert "offline_access" in doc["scopes_supported"]
        assert doc["bearer_methods_supported"] == ["header"]
        assert doc["resource"].endswith("/mcp")
        # §V12: the discovery document leaks no secret and never proxies AS metadata.
        assert "oauth-authorization-server" not in resp.text
        assert issuer.settings.jwks_url not in resp.text


def test_mcp_still_requires_bearer_and_points_at_metadata(
    secured_server: tuple[str, LocalOidcIssuer],
) -> None:
    # §V10 preserved: opening discovery did NOT open /mcp -- an unauthenticated POST is
    # still 401, and the challenge carries the RFC 9728 §5.1 resource_metadata hint so
    # a client can bootstrap from the 401 alone.
    url, _issuer = secured_server
    resp = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}, timeout=30)
    assert resp.status_code == 401
    challenge = resp.headers.get("www-authenticate", "")
    assert 'error="invalid_token"' in challenge
    assert "resource_metadata=" in challenge
    assert f"{_WELL_KNOWN}/mcp" in challenge


def test_token_without_advertised_flow_scope_is_accepted(
    secured_server: tuple[str, LocalOidcIssuer],
) -> None:
    # §V45/§V10 (B126): `offline_access` is ADVERTISED, never REQUIRED -- the AS turns
    # it into a refresh token and never mints it into the access token. So a bearer
    # granting only `arknights:read` must still be accepted over the wire; had the fix
    # instead appended the scope to `required_scopes`, this exact token -- the only
    # kind Auth0 issues -- would 403, turning fail-closed into a total outage.
    url, issuer = secured_server
    claims = jwt.decode(issuer.mint(), options={"verify_signature": False})
    assert "offline_access" not in claims["scope"]
    assert "offline_access" in issuer.settings.advertised_scopes

    resp = httpx.post(
        url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
        headers={
            "Authorization": f"Bearer {issuer.mint()}",
            "Accept": "application/json, text/event-stream",
        },
        timeout=30,
    )
    assert resp.status_code == 200


def test_discovery_isolated_per_server(tmp_path: Path) -> None:
    # A second server on its own port also serves discovery -- no shared module state
    # leaks the document across deployments (the doc is built per wrap_remote_app call).
    with remote_server(tmp_path) as (url, issuer):
        resp = httpx.get(_origin(url) + _WELL_KNOWN, timeout=30)
        assert resp.status_code == 200
        assert resp.json()["authorization_servers"] == [issuer.settings.issuer]
