"""Cloudflare Access sign-in: trust a request only if it carries a valid Access token.

Cloudflare Access sits in front of the app (through the tunnel), signs people in, and adds a signed JWT in the
`Cf-Access-Jwt-Assertion` header. We check the signature against Cloudflare's published keys, plus audience,
issuer and expiry, so a request that somehow reached the app without going through Access is still refused.
"""
from __future__ import annotations

import jwt  # PyJWT

HEADER = "cf-access-jwt-assertion"


class AccessVerifier:
    def __init__(self, team_domain: str, audience: str, emails: set[str] | None = None, key_client=None):
        team = team_domain.strip().removeprefix("https://").rstrip("/")
        if "." not in team:
            team = f"{team}.cloudflareaccess.com"
        self.issuer = f"https://{team}"
        self.audience = audience
        self.emails = {e.lower() for e in (emails or set())}
        # PyJWKClient fetches and caches Cloudflare's public keys (rotated periodically).
        self._keys = key_client or jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600)

    def verify(self, token: str | None) -> dict | None:
        """Return the token's claims if it is valid and allowed, else None."""
        if not token:
            return None
        try:
            key = self._keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.audience, issuer=self.issuer,
                                options={"require": ["exp", "iat", "aud", "iss"]})
        except Exception:
            return None
        if self.emails and not is_service_token(claims) and str(claims.get("email", "")).lower() not in self.emails:
            return None
        return claims


def is_service_token(claims: dict) -> bool:
    """Requests authenticated by an Access service token (scripts, the iPhone Shortcut) carry no user email."""
    return bool(claims.get("common_name")) and not claims.get("email")
