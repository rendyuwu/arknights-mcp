"""§T52/§V10: OIDC resource-server bearer validation.

Drives :class:`~arknights_mcp.auth.oidc.OidcTokenVerifier` with a local RS256
keypair and an injected JWKS resolver (no network), covering every §V10 rule:
RS256-only (no ``none``/HS* key confusion), exact issuer, string-or-array audience,
required registered claims, 60s leeway, ``iss|sub`` identity, ``azp`` client id, and
the typed ``invalid_token`` / ``insufficient_scope`` rejections.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from typing import Any

import anyio
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from arknights_mcp.auth.oidc import AuthError, OidcSettings, OidcTokenVerifier

ISSUER = "https://issuer.example.com/"
AUDIENCE = "arknights-mcp"
JWKS_URL = "https://issuer.example.com/.well-known/jwks.json"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()
_PUBLIC_PEM = _PUBLIC_KEY.public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
)


class _StaticJWKS:
    """Injected JWKS resolver returning the one test public key for any token."""

    def get_signing_key_from_jwt(self, token: str) -> Any:
        return SimpleNamespace(key=_PUBLIC_KEY)


class _RaisingJWKS:
    """Resolver that fails to find the key (unknown ``kid`` / JWKS error)."""

    def get_signing_key_from_jwt(self, token: str) -> Any:
        raise jwt.PyJWKClientError("no matching kid")


def _settings(required: tuple[str, ...] = ("arknights:read",)) -> OidcSettings:
    return OidcSettings(
        issuer=ISSUER, audience=AUDIENCE, jwks_url=JWKS_URL, required_scopes=required
    )


def _verifier(
    *, required: tuple[str, ...] = ("arknights:read",), jwks: Any | None = None
) -> OidcTokenVerifier:
    return OidcTokenVerifier(_settings(required), jwks_client=jwks or _StaticJWKS())


def _payload(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "sub": "auth0|user123",
        "aud": AUDIENCE,
        "exp": now + 3600,
        "iat": now,
        "azp": "client-abc",
        "scope": "arknights:read",
    }
    payload.update(overrides)
    return payload


def _token(*, algorithm: str = "RS256", key: Any = _PRIVATE_KEY, **overrides: Any) -> str:
    payload = _payload(**overrides)
    # Drop keys explicitly set to the sentinel so "missing claim" cases can omit them.
    payload = {k: v for k, v in payload.items() if v is not _OMIT}
    return jwt.encode(payload, key, algorithm=algorithm)


_OMIT = object()


def test_valid_token_yields_principal() -> None:
    principal = _verifier().verify(_token())
    assert principal.issuer == ISSUER
    assert principal.subject == "auth0|user123"
    assert principal.client_id == "client-abc"
    assert principal.scopes == frozenset({"arknights:read"})
    # §V10: identity is namespaced iss|sub (sub unique only per issuer).
    assert principal.principal_id == f"{ISSUER}|auth0|user123"


def test_expired_token_beyond_leeway_rejected() -> None:
    now = int(time.time())
    with pytest.raises(AuthError) as exc:
        _verifier().verify(_token(exp=now - 120, iat=now - 3600))
    assert exc.value.error == "invalid_token"
    assert exc.value.status == 401


def test_expired_within_leeway_accepted() -> None:
    # §V10: 60s leeway absorbs small clock skew.
    now = int(time.time())
    principal = _verifier().verify(_token(exp=now - 30, iat=now - 90))
    assert principal.subject == "auth0|user123"


def test_wrong_issuer_rejected() -> None:
    with pytest.raises(AuthError) as exc:
        _verifier().verify(_token(iss="https://evil.example.com/"))
    assert exc.value.error == "invalid_token"


def test_issuer_trailing_slash_is_exact() -> None:
    # §V10: issuer matched exactly, trailing slash included.
    with pytest.raises(AuthError):
        _verifier().verify(_token(iss=ISSUER.rstrip("/")))


def test_wrong_audience_rejected() -> None:
    with pytest.raises(AuthError) as exc:
        _verifier().verify(_token(aud="some-other-api"))
    assert exc.value.error == "invalid_token"


def test_audience_as_array_containing_expected_accepted() -> None:
    # §V10: Auth0 emits aud as an array once openid is added → accept both shapes.
    principal = _verifier().verify(_token(aud=[AUDIENCE, "https://other.api"]))
    assert principal.subject == "auth0|user123"


def test_audience_array_without_expected_rejected() -> None:
    with pytest.raises(AuthError):
        _verifier().verify(_token(aud=["x", "y"]))


def _b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _forge_hs256(payload: dict[str, Any], secret: bytes) -> str:
    """Hand-craft an HS256 JWS signed with ``secret``.

    PyJWT >=2.10 refuses to *encode* HS256 with a PEM key (it blocks the confusion
    attack at the encode side too), so the attacker's forged token is assembled
    manually to exercise our decode-side ``algorithms=["RS256"]`` defense.
    """
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(payload).encode())
    signing_input = header + b"." + body
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + _b64url(signature)).decode("ascii")


def test_hs256_key_confusion_rejected() -> None:
    # §V10: an HS256 token signed with the public key PEM as the shared secret must
    # NOT validate -- algorithms=["RS256"] forecloses symmetric-key confusion.
    forged = _forge_hs256(_payload(), _PUBLIC_PEM)
    with pytest.raises(AuthError) as exc:
        _verifier().verify(forged)
    assert exc.value.error == "invalid_token"


def test_alg_none_rejected() -> None:
    # §V10: alg=none must be rejected (no unsigned tokens).
    unsigned = jwt.encode(_payload(), key="", algorithm="none")
    with pytest.raises(AuthError) as exc:
        _verifier().verify(unsigned)
    assert exc.value.error == "invalid_token"


def test_missing_exp_rejected() -> None:
    with pytest.raises(AuthError) as exc:
        _verifier().verify(_token(exp=_OMIT))
    assert exc.value.error == "invalid_token"


def test_missing_iat_rejected() -> None:
    with pytest.raises(AuthError):
        _verifier().verify(_token(iat=_OMIT))


def test_unknown_kid_rejected() -> None:
    with pytest.raises(AuthError) as exc:
        _verifier(jwks=_RaisingJWKS()).verify(_token())
    assert exc.value.error == "invalid_token"


def test_insufficient_scope_is_typed_403() -> None:
    with pytest.raises(AuthError) as exc:
        _verifier().verify(_token(scope="other:read"))
    assert exc.value.error == "insufficient_scope"
    assert exc.value.status == 403


def test_scope_from_permissions_array_only() -> None:
    # §V10: granted authority = scope ∪ permissions; Auth0 M2M may emit permissions.
    principal = _verifier().verify(_token(scope=_OMIT, permissions=["arknights:read"]))
    assert principal.scopes == frozenset({"arknights:read"})


def test_required_scopes_are_anded_across_sources() -> None:
    # §V10: every required scope must be present; the union spans scope+permissions.
    verifier = _verifier(required=("arknights:read", "arknights:stages"))
    with pytest.raises(AuthError):
        verifier.verify(_token(scope="arknights:read"))  # missing arknights:stages
    principal = verifier.verify(_token(scope="arknights:read", permissions=["arknights:stages"]))
    assert {"arknights:read", "arknights:stages"} <= principal.scopes


def test_missing_azp_yields_none_client_id() -> None:
    principal = _verifier().verify(_token(azp=_OMIT))
    assert principal.client_id is None


def test_auth_error_message_carries_no_token() -> None:
    # §V10/§V12: rejection descriptions never contain the token or a secret.
    token = _token(iss="https://evil.example.com/")
    with pytest.raises(AuthError) as exc:
        _verifier().verify(token)
    assert token not in exc.value.description
    assert "eyJ" not in exc.value.description  # no JWT segment leaked


def test_verify_token_adapter_valid_returns_access_token() -> None:
    verifier = _verifier()
    token = _token()
    access = anyio.run(verifier.verify_token, token)
    assert access is not None
    assert access.subject == "auth0|user123"
    assert access.client_id == "client-abc"
    # §V12: the SDK AccessToken never echoes the raw bearer.
    assert access.token == ""


def test_verify_token_adapter_invalid_returns_none() -> None:
    # §V10: "return None past auth backend" on any validation failure.
    verifier = _verifier()
    token = _token(scope="other:read")
    assert anyio.run(verifier.verify_token, token) is None
