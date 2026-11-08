"""OAuth resource-server validation; the configured identity provider owns sign-in."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import json
import os
import time
from urllib.parse import urlsplit

import httpx
import jwt

OAUTH_SCOPE = "options:read"
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"


def https_url(value: str, name: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname and parsed.port in (None, 443)
                 and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                 and value.isascii() and not any(c.isspace() or c in '\\"<>' for c in value))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{name} must be an HTTPS URL without credentials, query, fragment or a non-443 port")
    return value


@dataclass(frozen=True)
class OAuthSettings:
    issuer: str
    jwks_url: str
    resource: str
    allowed_subjects: frozenset[str]
    algorithm: str

    @classmethod
    def from_env(cls) -> OAuthSettings:
        issuer = https_url(os.getenv("MCP_OAUTH_ISSUER", ""), "MCP_OAUTH_ISSUER")
        jwks_url = https_url(os.getenv("MCP_OAUTH_JWKS_URL", ""), "MCP_OAUTH_JWKS_URL")
        resource_url = os.getenv("MCP_RESOURCE_URL", "")
        if not resource_url and (render_host := os.getenv("RENDER_EXTERNAL_HOSTNAME", "")):
            # Use trusted deployment configuration, never the request Host header.
            resource_url = f"https://{render_host}/mcp"
        resource = https_url(resource_url, "MCP_RESOURCE_URL")
        if urlsplit(resource).path != "/mcp":
            raise ValueError("MCP_RESOURCE_URL must end in /mcp, without a trailing slash")
        subjects = frozenset(s.strip() for s in os.getenv("MCP_OAUTH_ALLOWED_SUBJECTS", "").split(",") if s.strip())
        if not subjects or "*" in subjects:
            raise ValueError("Set MCP_OAUTH_ALLOWED_SUBJECTS to explicit comma-separated identity-provider subject IDs")
        algorithm = os.getenv("MCP_OAUTH_ALGORITHM", "RS256")
        if algorithm not in {"RS256", "ES256"}:
            raise ValueError("MCP_OAUTH_ALGORITHM must be RS256 or ES256")
        return cls(issuer, jwks_url, resource, subjects, algorithm)

    @property
    def metadata_url(self) -> str:
        parsed = urlsplit(self.resource)
        return f"{parsed.scheme}://{parsed.netloc}{METADATA_PATH}"

    def metadata(self) -> dict:
        return {"resource": self.resource, "authorization_servers": [self.issuer],
                "scopes_supported": [OAUTH_SCOPE], "bearer_methods_supported": ["header"]}


class OAuthFailure(Exception):
    def __init__(self, status: int, error: str):
        self.status, self.error = status, error
        super().__init__(error)


class OAuthVerifier:
    """Verify JWTs against a pinned issuer/JWKS URL, with bounded key refreshes."""

    def __init__(self, settings: OAuthSettings):
        self.settings = settings
        self._keys: list[jwt.PyJWK] = []
        self._expires_at = 0.0
        self._last_attempt = -math.inf
        self._lock = asyncio.Lock()

    async def _fetch_keys(self) -> list[jwt.PyJWK]:
        # Never follow token-supplied jku/x5u URLs or forward the user's token.
        async with asyncio.timeout(5), httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            async with client.stream("GET", self.settings.jwks_url) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 262144:
                        raise ValueError("JWKS response too large")
        document = json.loads(data)
        if (not isinstance(document, dict) or not isinstance(document.get("keys"), list)
                or not 1 <= len(document["keys"]) <= 128
                or any(not isinstance(key, dict) for key in document["keys"])):
            raise ValueError("Invalid JWKS")
        return jwt.PyJWKSet.from_dict(document).keys

    def _matching_key(self, kid: str) -> jwt.PyJWK | None:
        keys = [key for key in self._keys if key.key_id == kid
                and key.algorithm_name == self.settings.algorithm and key.public_key_use in (None, "sig")]
        return keys[0] if len(keys) == 1 else None

    async def _signing_key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            now = time.monotonic()
            key = self._matching_key(kid)
            fresh = now < self._expires_at
            if key is not None and fresh:
                return key
            # Unknown kids cannot force an unbounded outbound request per token.
            if now - self._last_attempt >= 30:
                self._last_attempt = now
                try:
                    keys = await self._fetch_keys()
                    if not keys:
                        raise ValueError("Empty JWKS")
                except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError, jwt.PyJWTError):
                    raise OAuthFailure(503, "temporarily_unavailable") from None
                self._keys = keys
                self._expires_at = time.monotonic() + 300
                key = self._matching_key(kid)
            elif not fresh:
                raise OAuthFailure(503, "temporarily_unavailable")
            if key is None:
                raise OAuthFailure(401, "invalid_token")
            return key

    async def verify(self, token: str) -> None:
        try:
            if len(token) > 16384:
                raise jwt.InvalidTokenError()
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != self.settings.algorithm or not isinstance(kid, str) or not kid or len(kid) > 256:
                raise jwt.InvalidTokenError()
            key = await self._signing_key(kid)
            claims = jwt.decode(token, key.key, algorithms=[self.settings.algorithm],
                                issuer=self.settings.issuer, audience=self.settings.resource,
                                options={"require": ["iss", "aud", "exp", "iat", "sub"]})
            # Numeric dates must be finite numbers, not strings/bools or NaN.
            for field in ("exp", "iat", "nbf"):
                if field in claims and (type(claims[field]) not in (int, float) or not math.isfinite(claims[field])):
                    raise jwt.InvalidTokenError()
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
            raise OAuthFailure(401, "invalid_token") from None
        if claims["sub"] not in self.settings.allowed_subjects:
            raise OAuthFailure(403, "access_denied")
        scope = claims.get("scope")
        if not isinstance(scope, str) or OAUTH_SCOPE not in scope.split():
            raise OAuthFailure(403, "insufficient_scope")
