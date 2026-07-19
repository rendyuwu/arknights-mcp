"""Authenticated principal derived from a validated bearer token (§V10).

A :class:`Principal` is the immutable identity the remote transport attaches to a
request once :mod:`arknights_mcp.auth.oidc` has validated the token. It carries
only the claims the server needs -- issuer, subject, client id, granted scopes --
never the raw token or any secret (§V10/§V12).

Identity key (§V10): the OIDC ``sub`` claim is unique only *within* an issuer, so a
principal is keyed by ``iss|sub`` -- two issuers may mint the same ``sub``. Session
isolation (§T53) keys per-principal state on :attr:`Principal.principal_id`, so the
namespacing must live here in one home (§V37).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Principal:
    """Immutable identity for a validated remote caller (§V10).

    :param issuer: the token ``iss`` claim (exact, includes any trailing slash).
    :param subject: the token ``sub`` claim (unique only per issuer).
    :param client_id: the OAuth client (``azp``); ``None`` when the token omits it.
    :param scopes: the granted scopes (``scope`` string ∪ ``permissions`` array).
    """

    issuer: str
    subject: str
    client_id: str | None
    scopes: frozenset[str]

    @property
    def principal_id(self) -> str:
        """Stable per-issuer identity key ``iss|sub`` (§V10).

        ``sub`` alone is not globally unique -- two issuers may emit the same
        subject -- so per-principal state (rate limits §V11, session cache §T53)
        must key on this namespaced value, never on ``sub`` alone.
        """
        return f"{self.issuer}|{self.subject}"
