"""OIDC resource-server bearer validation (remote transport only; §V10).

This is the resource-server half of an AS/RS split: an external OpenID provider
(verified against a live Auth0 M2M token, §V10) mints the token; we only *validate*
it. Validation is deliberately strict and fail-closed:

* **RS256 only** -- ``algorithms=["RS256"]`` rejects ``none`` and every HS* variant,
  so an attacker cannot present an HMAC token signed with the (public) JWKS key and
  have it accepted as a signature (the classic key-confusion attack, §V10).
* **JWKS by ``kid``** -- :class:`jwt.PyJWKClient` selects the signing key by the
  token header's ``kid`` and caches the JWKS; an unknown ``kid`` triggers a refetch,
  so provider key rotation is picked up without a restart (§V10).
* **Registered claims required** -- ``exp``/``iat``/``iss``/``aud`` must be present;
  ``iss`` is matched exactly (trailing slash included) and ``aud`` accepts a string
  *or* an array (Auth0 emits an array once ``openid`` is added). A 60s ``leeway``
  absorbs small clock skew (§V10).
* **Scope is AND** -- every required scope must be granted; granted authority is the
  union of ``scope`` and ``permissions`` (:mod:`arknights_mcp.auth.scopes`, §V10).

Failures never leak the token or a secret (§V10/§V12): :class:`AuthError` carries a
typed OAuth error code (``invalid_token`` / ``insufficient_scope``) and a static,
token-free description for the ``WWW-Authenticate`` challenge. The SDK-protocol
adapter :meth:`OidcTokenVerifier.verify_token` collapses any failure to ``None``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import anyio
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken

from arknights_mcp.auth.principal import Principal
from arknights_mcp.auth.scopes import granted_scopes, has_required_scopes
from arknights_mcp.util.text import is_placeholder

if TYPE_CHECKING:
    from arknights_mcp.config import AuthConfig

#: Registered claims a token must carry (§V10). ``iss``/``aud`` are also value-matched.
_REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud")

#: Only RS256 (§V10): rejects ``none`` and HS* → no symmetric-key confusion.
_ALGORITHMS = ["RS256"]

#: Clock-skew tolerance for ``exp``/``iat`` in seconds (§V10).
_LEEWAY_SECONDS = 60


@dataclass(frozen=True, slots=True)
class AuthError(Exception):
    """A typed, token-free bearer rejection (§V10/§V12).

    :param error: RFC 6750 challenge code -- ``invalid_token`` or
        ``insufficient_scope``.
    :param status: HTTP status for the challenge (401 or 403).
    :param description: a static, safe reason; never contains the token or a secret.
    """

    error: str
    status: int
    description: str


@dataclass(frozen=True, slots=True)
class OidcSettings:
    """Non-secret OIDC descriptors needed to validate a token (§V10).

    Built from ``[auth]`` config overlaid with env descriptors (§I.env). All four
    fields must be concrete (non-placeholder) -- the §V9 startup gate guarantees
    that before a verifier is constructed; :meth:`from_auth_config` re-checks and
    fails closed.
    """

    issuer: str
    audience: str
    jwks_url: str
    required_scopes: tuple[str, ...]

    @classmethod
    def from_auth_config(cls, auth: AuthConfig) -> OidcSettings:
        """Build settings from a validated ``[auth]`` config; fail closed if not.

        Raises :class:`ValueError` when a descriptor is missing or a ``<...>``
        placeholder, mirroring :attr:`~arknights_mcp.config.AuthConfig.is_valid_oidc`
        so a verifier is never constructed from an unconfigured provider.
        """
        if (
            auth.mode != "oidc"
            or is_placeholder(auth.issuer)
            or is_placeholder(auth.audience)
            or is_placeholder(auth.jwks_url)
            or not auth.required_scopes
        ):
            raise ValueError("OIDC settings incomplete: issuer/audience/jwks_url/required_scopes")
        # Narrowed to str by the is_placeholder guards above.
        assert auth.issuer is not None and auth.audience is not None and auth.jwks_url is not None
        return cls(
            issuer=auth.issuer,
            audience=auth.audience,
            jwks_url=auth.jwks_url,
            required_scopes=tuple(auth.required_scopes),
        )


class _SigningKeyResolver(Protocol):
    """Structural type for the JWKS key lookup (:class:`jwt.PyJWKClient` / test stub)."""

    def get_signing_key_from_jwt(self, token: str) -> Any: ...  # pragma: no cover


class OidcTokenVerifier:
    """Validate bearer tokens against an OIDC provider's JWKS (§V10).

    ``jwks_client`` is injectable so tests drive validation with a local RS256
    keypair (no network); production passes ``None`` and a cached
    :class:`jwt.PyJWKClient` is created from :attr:`OidcSettings.jwks_url`.
    """

    def __init__(
        self,
        settings: OidcSettings,
        *,
        jwks_client: _SigningKeyResolver | None = None,
        leeway_seconds: int = _LEEWAY_SECONDS,
    ) -> None:
        self._settings = settings
        self._leeway = leeway_seconds
        # Lazy fetch: PyJWKClient does not hit the network until first use, so
        # construction stays offline (§V1 does not gate auth infra, but a startup
        # network call would be surprising).
        self._jwks_client: _SigningKeyResolver = jwks_client or PyJWKClient(settings.jwks_url)

    def verify(self, token: str) -> Principal:
        """Validate ``token`` → :class:`Principal`, else raise :class:`AuthError`.

        Synchronous (PyJWT + JWKS fetch are blocking); the async transport calls it
        off the event loop via :meth:`verify_token`.
        """
        signing_key = self._resolve_key(token)
        claims = self._decode(token, signing_key)
        subject = claims.get("sub")
        issuer = claims.get("iss")
        if not isinstance(subject, str) or not subject or not isinstance(issuer, str) or not issuer:
            raise AuthError("invalid_token", 401, "token subject or issuer missing")
        granted = granted_scopes(claims)
        if not has_required_scopes(granted, self._settings.required_scopes):
            # Typed scope reject distinct from a bad token (§V10): 403, not 401.
            raise AuthError("insufficient_scope", 403, "required scope not granted")
        azp = claims.get("azp")
        client_id = azp if isinstance(azp, str) else None
        return Principal(
            issuer=issuer,
            subject=subject,
            client_id=client_id,
            scopes=granted,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """SDK ``TokenVerifier`` adapter: valid → :class:`AccessToken`, else ``None``.

        §V10 "return None past auth backend": any validation failure collapses to
        ``None`` (no principal). The blocking :meth:`verify` runs in a worker thread
        so it never stalls the event loop. The echoed :class:`AccessToken` never
        carries the raw token (§V12).
        """
        try:
            principal = await anyio.to_thread.run_sync(self.verify, token)
        except AuthError:
            return None
        return AccessToken(
            token="",  # never echo the bearer (§V12)
            client_id=principal.client_id or "",
            scopes=sorted(principal.scopes),
            subject=principal.subject,
            claims={"iss": principal.issuer},
        )

    def _resolve_key(self, token: str) -> Any:
        """Select the JWKS signing key by header ``kid`` (cache + rotation, §V10)."""
        try:
            return self._jwks_client.get_signing_key_from_jwt(token).key
        except (jwt.PyJWTError, OSError) as exc:
            # Unknown kid, malformed header, or JWKS fetch failure → deny, no leak.
            raise AuthError("invalid_token", 401, "unable to resolve token signing key") from exc

    def _decode(self, token: str, signing_key: Any) -> dict[str, Any]:
        """Decode + validate signature/iss/aud/exp/iat (RS256 only, §V10)."""
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key,
                algorithms=_ALGORITHMS,
                audience=self._settings.audience,
                issuer=self._settings.issuer,
                leeway=self._leeway,
                options={"require": list(_REQUIRED_CLAIMS)},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("invalid_token", 401, "token expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError("invalid_token", 401, "token audience invalid") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthError("invalid_token", 401, "token issuer invalid") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise AuthError("invalid_token", 401, "token missing a required claim") from exc
        except jwt.PyJWTError as exc:
            # Bad signature, wrong alg (none/HS*), malformed token, etc.
            raise AuthError("invalid_token", 401, "token invalid") from exc
        return claims


def required_scopes_from(settings: OidcSettings) -> Sequence[str]:
    """Expose the configured required scopes for a ``WWW-Authenticate`` ``scope=``."""
    return settings.required_scopes
