"""
Who may call the API. Every request is checked in this order: client address, Host, Origin, then auth.

    Host     must be 127.0.0.1:<port> or localhost:<port>, the port being the one uvicorn is bound to.
             This stops DNS rebinding. Anything else is 403. "testserver" only when test_mode is set.
    Origin   on a non-safe request, if present, must be this app's own origin (a Vite dev origin only in dev mode).
    auth     a non-safe request needs either
               Authorization: Bearer <JOBPILOT_API_TOKEN>   -> caller "agent"   (wrong or unusable bearer: 401)
               X-JobPilot-Client: dashboard                 -> caller "dashboard"
             otherwise 403. The dashboard header is a CSRF guard, not authentication: any local process can send it.
             The token only identifies agents; it does not decide whether a run may submit (the run request does).

GET, HEAD and OPTIONS are safe: they need only the Host check.
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from dotenv import dotenv_values
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("jobpilot.api.security")

TOKEN_ENV = "JOBPILOT_API_TOKEN"
MIN_TOKEN_LENGTH = 32
CLIENT_HEADER = "x-jobpilot-client"
CLIENT_VALUE = "dashboard"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def load_api_token(env_file: Path | None = None) -> str:
    """The token from the environment, else from the one key in .env. Never logged, never returned to a client."""
    value = os.environ.get(TOKEN_ENV, "")
    if not value and env_file is not None and env_file.is_file():
        value = dotenv_values(env_file).get(TOKEN_ENV) or ""
    return value.strip()


def usable_token(token: str | None, warn: bool = True) -> str:
    """The token if it is long enough, else "" (token auth off). Warns once, without the value or its length."""
    token = (token or "").strip()
    if len(token) >= MIN_TOKEN_LENGTH:
        return token
    if warn:
            log.warning("%s is missing or too short (it needs at least %d characters): API token auth is disabled, "
                    "the dashboard still works.", TOKEN_ENV, MIN_TOKEN_LENGTH)
    return ""


def own_hosts(port: int | None, test_mode: bool) -> set[str]:
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"} if port else set()
    return hosts | ({"testserver"} if test_mode else set())


def own_origins(port: int | None, test_mode: bool, dev_mode: bool) -> set[str]:
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"} if port else set()
    if test_mode:
        origins.add("http://testserver")
    if dev_mode:
        origins.update(DEV_ORIGINS)
    return origins


def bearer_of(value: str) -> str | None:
    """The token in an "Authorization: Bearer <token>" value, or None if it is not that shape."""
    scheme, _, token = value.partition(" ")
    return token if scheme.lower() == "bearer" and token and " " not in token else None


class RequestGuard:
    """Pure ASGI middleware (it must not buffer the SSE stream). Sets scope["state"]["caller"] on allowed non-safe requests."""

    def __init__(self, app: ASGIApp, token: str = "", test_mode: bool = False, dev_mode: bool = False) -> None:
        self.app = app
        self.token = token.encode()
        self.test_mode = test_mode
        self.dev_mode = dev_mode

    @staticmethod
    async def refuse(scope: Scope, receive: Receive, send: Send, status: int, detail: str, headers: dict | None = None) -> None:
        await JSONResponse({"detail": detail}, status_code=status, headers=headers)(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":   # the API has none; never let one in
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        if (client[0] if client else "") not in LOOPBACK:
            # Defence in depth: uvicorn is bound to 127.0.0.1, but never serve another machine.
            await self.refuse(scope, receive, send, 403, "JobPilot only serves this computer")
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        server = scope.get("server")
        port = server[1] if server else None

        if headers.get("host", "").lower() not in own_hosts(port, self.test_mode):
            await self.refuse(scope, receive, send, 403, "Host not allowed")
            return
        if scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        origin = headers.get("origin")
        if origin is not None and origin.lower() not in own_origins(port, self.test_mode, self.dev_mode):
            await self.refuse(scope, receive, send, 403, "Origin not allowed")
            return

        caller = self.authenticate(headers)
        if isinstance(caller, int):
            detail = "API token not accepted" if caller == 401 else "This request needs the dashboard header or an API token"
            await self.refuse(scope, receive, send, caller, detail, {"WWW-Authenticate": "Bearer"} if caller == 401 else None)
            return
        scope.setdefault("state", {})["caller"] = caller
        await self.app(scope, receive, send)

    def authenticate(self, headers: dict[str, str]) -> str | int:
        """The caller ("agent" or "dashboard") or the status code to refuse with. A wrong bearer never falls back to the header."""
        if "authorization" in headers:
            presented = bearer_of(headers["authorization"])
            if presented is not None and self.token and hmac.compare_digest(presented.encode(), self.token):
                return "agent"
            return 401
        return "dashboard" if headers.get(CLIENT_HEADER) == CLIENT_VALUE else 403
