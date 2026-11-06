"""Authenticated ASGI entrypoint for Render and other Python/container hosts."""
import hmac
import os
import re

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

from main import mcp


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
    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def private_send(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (b"cache-control", b"no-store")]}
            await send(message)

        public_health = scope["path"] == "/health" and scope["method"] in ("GET", "HEAD")
        if not public_health:
            values = [value for name, value in scope.get("headers", []) if name.lower() == b"authorization"]
            parts = values[0].split() if len(values) == 1 else []
            authorized = (len(parts) == 2 and parts[0].lower() == b"bearer"
                          and hmac.compare_digest(parts[1], self.token))
            if not authorized:
                response = JSONResponse({"error": "Unauthorized"}, status_code=401,
                                        headers={"WWW-Authenticate": "Bearer"})
                return await response(scope, receive, private_send)
        await self.app(scope, receive, private_send)


async def health(request):
    # Liveness only: no provider call, credentials, account details or dependency versions.
    return JSONResponse({"status": "ok"})


def create_app():
    token = os.getenv("MCP_API_KEY", "")
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError("Set MCP_API_KEY to a separate random token of at least 32 characters, without whitespace")
    if not os.getenv("THETADATA_API_KEY"):
        raise ValueError("Set THETADATA_API_KEY in the service environment before starting HTTP mode")
    mcp.settings.transport_security = transport_security()
    # One factory instance per process. Preserve the SDK's session-manager lifespan.
    app = mcp.streamable_http_app()
    app.router.routes.append(Route("/health", health, methods=["GET", "HEAD"]))
    return BearerTokenMiddleware(app, token)
