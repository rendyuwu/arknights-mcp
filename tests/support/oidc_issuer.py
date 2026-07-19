"""A local, offline OIDC token issuer for remote-transport tests (§V10).

Stands in for the external OpenID provider (Auth0 in the verified §V10 reference):
holds one RSA keypair, mints RS256 bearer tokens the real
:class:`~arknights_mcp.auth.oidc.OidcTokenVerifier` accepts, and exposes a static
JWKS resolver so the verifier validates the signature without touching the network.

One home (§V37) for the honest-token substrate the authenticated remote tests need:
build the issuer, hand its :attr:`settings` to the app under test and its
:attr:`jwks_resolver` to the verifier, then :meth:`mint` a bearer. The verifier's
``jwks_client`` seam (a resolver that returns an object with a ``.key``) is exactly
what this feeds, so the same real decode/validate path runs -- only the key fetch is
local. Attack-shaped tokens (``alg=none``, HS256 confusion, omitted claims) are the
adversarial suites' own concern and deliberately not modeled here.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from arknights_mcp.auth.oidc import OidcSettings

#: Defaults for a minted token; every field is overridable per :meth:`LocalOidcIssuer.mint`.
_DEFAULT_ISSUER = "https://issuer.test.local/"
_DEFAULT_AUDIENCE = "arknights-mcp"
_DEFAULT_JWKS_URL = "https://issuer.test.local/.well-known/jwks.json"
_DEFAULT_SUBJECT = "auth0|remote-tester"
_DEFAULT_CLIENT_ID = "client-remote-test"
_DEFAULT_SCOPE = "arknights:read"


class LocalOidcIssuer:
    """An in-process RSA token issuer + matching JWKS resolver (§V10).

    :param issuer: token ``iss`` (also matched exactly by the verifier).
    :param audience: token ``aud`` (also the verifier's expected audience).
    :param required_scopes: scopes the verifier will require; the default minted
        token grants exactly these.
    """

    def __init__(
        self,
        *,
        issuer: str = _DEFAULT_ISSUER,
        audience: str = _DEFAULT_AUDIENCE,
        jwks_url: str = _DEFAULT_JWKS_URL,
        required_scopes: tuple[str, ...] = (_DEFAULT_SCOPE,),
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks_url = jwks_url
        self._required_scopes = required_scopes
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._public_key = self._private_key.public_key()

    @property
    def private_key(self) -> Any:
        """The RSA private key that signs minted tokens (RS256).

        Exposed so adversarial suites can build their own attack-shaped tokens
        (``alg=none``, HS256 confusion, omitted claims) on the same keypair the
        :attr:`jwks_resolver` validates against, without duplicating keygen (§V37).
        """
        return self._private_key

    @property
    def public_key(self) -> Any:
        """The RSA public key matching :attr:`private_key` (also served by the resolver)."""
        return self._public_key

    @property
    def settings(self) -> OidcSettings:
        """The :class:`OidcSettings` a verifier for this issuer must carry."""
        return OidcSettings(
            issuer=self._issuer,
            audience=self._audience,
            jwks_url=self._jwks_url,
            required_scopes=self._required_scopes,
        )

    @property
    def jwks_resolver(self) -> Any:
        """A JWKS resolver returning this issuer's public key for any token.

        Shaped for :class:`~arknights_mcp.auth.oidc.OidcTokenVerifier`'s
        ``jwks_client`` seam: ``get_signing_key_from_jwt`` yields an object whose
        ``.key`` is the RSA public key, so the real signature check runs offline.
        """
        public_key = self._public_key

        class _StaticJWKS:
            def get_signing_key_from_jwt(self, token: str) -> Any:
                return SimpleNamespace(key=public_key)

        return _StaticJWKS()

    def mint(self, **overrides: Any) -> str:
        """Mint an RS256 bearer with the granted scope, overridable per claim.

        The default token carries a valid ``iss``/``aud``/``exp``/``iat``/``sub``/
        ``azp`` and grants the issuer's required scopes -- i.e. the verifier accepts
        it. Pass e.g. ``scope=...`` or ``sub=...`` to shape a specific case.
        """
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self._issuer,
            "sub": _DEFAULT_SUBJECT,
            "aud": self._audience,
            "exp": now + 3600,
            "iat": now,
            "azp": _DEFAULT_CLIENT_ID,
            "scope": " ".join(self._required_scopes),
        }
        claims.update(overrides)
        return jwt.encode(claims, self._private_key, algorithm="RS256")
