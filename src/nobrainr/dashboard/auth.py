"""Basic auth middleware for dashboard routes (pure ASGI, SSE-safe)."""

import base64
import secrets

from nobrainr.config import settings

# Paths that skip auth (MCP protocol + static assets)
SKIP_PREFIXES = ("/sse", "/messages/", "/static")


class BasicAuthMiddleware:
    """HTTP Basic Auth — pure ASGI middleware (no BaseHTTPMiddleware).

    BaseHTTPMiddleware breaks SSE/streaming responses, so we implement
    the ASGI interface directly. Skips MCP and static paths.

    If dashboard credentials are not configured, all requests pass through
    (for VPN-only deployments where Traefik handles access control).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Skip auth for MCP protocol and static files
        if any(path.startswith(p) for p in SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return

        # If no credentials configured, pass through
        if not settings.dashboard_user or not settings.dashboard_password:
            await self.app(scope, receive, send)
            return

        # Check Authorization header
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")

        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                username, password = decoded.split(":", 1)
                if (
                    secrets.compare_digest(username, settings.dashboard_user)
                    and secrets.compare_digest(password, settings.dashboard_password)
                ):
                    await self.app(scope, receive, send)
                    return
            except Exception:
                pass

        # Send 401 response
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"www-authenticate", b'Basic realm="nobrainr"'),
                (b"content-type", b"text/plain"),
                (b"content-length", b"12"),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": b"Unauthorized",
        })
