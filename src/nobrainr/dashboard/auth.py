"""Basic auth middleware for dashboard routes."""

import base64
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from nobrainr.config import settings

# Paths that skip auth (MCP protocol + static assets)
SKIP_PREFIXES = ("/sse", "/messages/", "/static")


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic Auth middleware. Skips MCP and static paths.

    If dashboard credentials are not configured, all requests pass through
    (for VPN-only deployments where Traefik handles access control).
    """

    async def dispatch(self, request: Request, call_next):
        # Skip auth for MCP protocol and static files
        path = request.url.path
        if any(path.startswith(p) for p in SKIP_PREFIXES):
            return await call_next(request)

        # If no credentials configured, pass through
        if not settings.dashboard_user or not settings.dashboard_password:
            return await call_next(request)

        # Check Authorization header
        auth = request.headers.get("Authorization")
        if auth and auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                username, password = decoded.split(":", 1)
                if (
                    secrets.compare_digest(username, settings.dashboard_user)
                    and secrets.compare_digest(password, settings.dashboard_password)
                ):
                    return await call_next(request)
            except Exception:
                pass

        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="nobrainr"'},
            content="Unauthorized",
        )
