"""OAuth/OIDC resource-server validation (remote transport only; §T52; §V9/§V10/§V40).

Three collaborating homes, each with one responsibility (§C separated layers):

* :mod:`~arknights_mcp.auth.oidc` -- validate a bearer token → :class:`Principal`;
* :mod:`~arknights_mcp.auth.principal` -- the immutable validated identity;
* :mod:`~arknights_mcp.auth.scopes` -- the ``scope``∪``permissions`` union + AND match.

The startup gate (§V9/§V40) lives in :mod:`arknights_mcp.config`; the wire-level
bearer enforcement (401/403 challenges) lives in the Streamable HTTP transport.
"""

from arknights_mcp.auth.oidc import AuthError, OidcSettings, OidcTokenVerifier
from arknights_mcp.auth.principal import Principal
from arknights_mcp.auth.scopes import granted_scopes, has_required_scopes, missing_scopes

__all__ = [
    "AuthError",
    "OidcSettings",
    "OidcTokenVerifier",
    "Principal",
    "granted_scopes",
    "has_required_scopes",
    "missing_scopes",
]
