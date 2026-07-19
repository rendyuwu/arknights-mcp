"""Required-scope enforcement for validated tokens (§V10).

Two OIDC providers disagree on where granted authority lives in an access token:
the OAuth ``scope`` claim is a single space-delimited string, while Auth0's
client-credentials tokens carry an Auth0-specific ``permissions`` array (and emit
``scope`` only after an API permission is granted to the M2M app). §V10 requires we
accept authority from *both* -- the granted set is their union.

Required-scope matching is AND (§V10): a caller is authorized only when *every*
required scope is present in the granted set. This is the single home (§V37) for
that policy; :mod:`arknights_mcp.auth.oidc` calls it, and nothing re-implements it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def granted_scopes(claims: Mapping[str, Any]) -> frozenset[str]:
    """Union of granted authority across ``scope`` and ``permissions`` (§V10).

    * ``scope`` -- a space-delimited string (standard OAuth); split on whitespace.
    * ``permissions`` -- an array of strings (Auth0 client-credentials).

    Absent or wrong-typed claims contribute nothing (fail-closed: an unparseable
    authority source grants no scope, never all).
    """
    granted: set[str] = set()
    scope = claims.get("scope")
    if isinstance(scope, str):
        granted.update(scope.split())
    permissions = claims.get("permissions")
    if isinstance(permissions, list):
        granted.update(item for item in permissions if isinstance(item, str))
    return frozenset(granted)


def missing_scopes(granted: Iterable[str], required: Iterable[str]) -> frozenset[str]:
    """Required scopes not present in ``granted`` (AND semantics, §V10)."""
    return frozenset(required) - frozenset(granted)


def has_required_scopes(granted: Iterable[str], required: Iterable[str]) -> bool:
    """True iff *every* required scope is granted (AND, §V10).

    An empty ``required`` set is trivially satisfied; the §V9 startup gate refuses a
    remote deployment whose ``required_scopes`` is empty, so this never authorizes an
    unscoped caller in a real remote run.
    """
    return not missing_scopes(granted, required)
