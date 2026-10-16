"""MCPServer factory and the bearer-token wrapper (same pattern as the Agent Hub's server)."""
from __future__ import annotations

import hmac

from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse

from jobpilot.mcp_server.api_client import ApiClient
from jobpilot.mcp_server.tools import register


def create_mcp_server(api: ApiClient) -> MCPServer:
    mcp = MCPServer("jobpilot")
    register(mcp, api)
    return mcp


class BearerTokenMiddleware:
    """Pure-ASGI static bearer-token check on every request. Fails closed."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"")
            supplied = auth[7:] if auth.lower().startswith(b"bearer ") else b""
            if not self.token or not hmac.compare_digest(supplied, self.token):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(api: ApiClient, token: str):
    app = create_mcp_server(api).streamable_http_app(host="127.0.0.1")
    app.add_middleware(BearerTokenMiddleware, token=token)
    return app
