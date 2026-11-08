"""Authenticated ASGI entrypoint for Render and other Python/container hosts."""
import hmac
import os
import re

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

from main import mcp
from oauth import METADATA_PATH, OAUTH_SCOPE, OAuthFailure, OAuthSettings, OAuthVerifier


def transport_security() -> TransportSecuritySettings:
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"]
    origins = ["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*"]
    configured = os.getenv("MCP_ALLOWED_HOSTS", "").split(",")
    configured.append(os.getenv("RENDER_EXTERNAL_HOSTNAME", ""))
    for host in configured:
        host = host.strip().lower()
        if not host:
            continue
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?", host):
            raise ValueError("MCP_ALLOWED_HOSTS must contain comma-separated hostnames, optionally with ports, without URLs or wildcards")
        hosts.append(host)
        if ":" not in host:
            hosts.append(host + ":443")
        origins.append("https://" + host)
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=sorted(set(hosts)), allowed_origins=sorted(set(origins)))


class BearerTokenMiddleware:
    def __init__(self, app, token: str = "", oauth: OAuthVerifier | None = None):
        self.app, self.token, self.oauth = app, token.encode(), oauth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def private_send(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (b"cache-control", b"no-store")]}
            await send(message)

        public_health = scope["path"] == "/health" and scope["method"] in ("GET", "HEAD")
        public_metadata = (self.oauth is not None and scope["method"] in ("GET", "HEAD")
                           and scope["path"] in (METADATA_PATH, "/.well-known/oauth-protected-resource"))
        if not (public_health or public_metadata):
            values = [value for name, value in scope.get("headers", []) if name.lower() == b"authorization"]
            parts = values[0].split() if len(values) == 1 else []
            status, error = 401, "Unauthorized"
            valid_header = len(parts) == 2 and parts[0].lower() == b"bearer"
            authorized = False
            challenge = "Bearer"
            if self.oauth:
                challenge += f' resource_metadata="{self.oauth.settings.metadata_url}", scope="{OAUTH_SCOPE}"'
                if valid_header:
                    try:
                        await self.oauth.verify(parts[1].decode("ascii"))
                        authorized = True
                    except UnicodeDecodeError:
                        error = "invalid_token"
                    except OAuthFailure as failure:
                        status, error = failure.status, failure.error
                    if not authorized and status != 503:
                        challenge += f', error="{error}"'
            else:
                authorized = valid_header and hmac.compare_digest(parts[1], self.token)
            if not authorized:
                headers = {"Retry-After": "30"} if status == 503 else {"WWW-Authenticate": challenge}
                response = JSONResponse({"error": error}, status_code=status, headers=headers)
                return await response(scope, receive, private_send)
        await self.app(scope, receive, private_send)


async def health(request):
    # Liveness only: no provider call, credentials, account details or dependency versions.
    return JSONResponse({"status": "ok"})


def create_app():
    mode = os.getenv("MCP_AUTH_MODE", "bearer")
    if mode not in {"bearer", "oauth"}:
        raise ValueError("MCP_AUTH_MODE must be bearer or oauth; anonymous HTTP access is not supported")
    oauth = OAuthVerifier(OAuthSettings.from_env()) if mode == "oauth" else None
    token = os.getenv("MCP_API_KEY", "") if mode == "bearer" else ""
    if mode == "bearer" and (len(token) < 32 or any(c.isspace() for c in token)):
        raise ValueError("Set MCP_API_KEY to a separate random token of at least 32 characters, without whitespace")
    if not os.getenv("THETADATA_API_KEY"):
        raise ValueError("Set THETADATA_API_KEY in the service environment before starting HTTP mode")
    mcp.settings.transport_security = transport_security()
    if oauth:
        from urllib.parse import urlsplit
        if urlsplit(oauth.settings.resource).netloc not in mcp.settings.transport_security.allowed_hosts:
            raise ValueError("MCP_RESOURCE_URL host must be in MCP_ALLOWED_HOSTS or match RENDER_EXTERNAL_HOSTNAME")
    mcp.oauth_enabled = oauth is not None
    # One factory instance per process. Preserve the SDK's session-manager lifespan.
    app = mcp.streamable_http_app()
    app.router.routes.append(Route("/health", health, methods=["GET", "HEAD"]))
    if oauth:
        async def metadata(request):
            return JSONResponse(oauth.settings.metadata())

        for path in (METADATA_PATH, "/.well-known/oauth-protected-resource"):
            app.router.routes.append(Route(path, metadata, methods=["GET", "HEAD"]))
    return BearerTokenMiddleware(app, token, oauth)
