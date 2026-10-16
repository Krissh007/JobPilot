"""HTTP client for JobPilot's own REST API. The MCP server's only way to read or change anything."""
from __future__ import annotations

from typing import Any

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"


class ApiError(Exception):
    """A failed API call. `say` is a sentence safe to read out loud."""

    def __init__(self, say: str, status: int | None = None):
        super().__init__(say)
        self.say = say
        self.status = status


def _detail_text(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, dict):
        detail = detail.get("message")
    if isinstance(detail, list):  # FastAPI validation errors
        detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    return str(detail) if detail else f"JobPilot returned an error ({response.status_code})."


class ApiClient:
    def __init__(self, base_url: str = DEFAULT_API_URL, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 15.0):
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/api", transport=transport, timeout=timeout)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TransportError:
            raise ApiError("JobPilot isn't running. Start it and try again.") from None
        if response.status_code >= 400:
            raise ApiError(_detail_text(response), response.status_code)
        return response.json()

    async def get(self, path: str, params: Any = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json: Any = None) -> Any:
        return await self._request("POST", path, json=json)

    async def aclose(self) -> None:
        await self._http.aclose()
