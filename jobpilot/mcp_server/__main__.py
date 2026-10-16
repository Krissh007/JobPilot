"""python -m jobpilot.mcp_server  (127.0.0.1:8766/mcp; needs JOBPILOT_MCP_TOKEN)."""
from __future__ import annotations

import os
import sys

import uvicorn
from dotenv import load_dotenv

from jobpilot.config import ROOT
from jobpilot.mcp_server.api_client import DEFAULT_API_URL, ApiClient
from jobpilot.mcp_server.server import create_app


def main() -> int:
    load_dotenv(ROOT / ".env")
    token = os.environ.get("JOBPILOT_MCP_TOKEN", "")
    if len(token) < 16:
        print("Set JOBPILOT_MCP_TOKEN (at least 16 characters) in .env or the environment.", file=sys.stderr)
        return 2
    api = ApiClient(os.environ.get("JOBPILOT_API_URL", DEFAULT_API_URL))
    uvicorn.run(create_app(api, token), host="127.0.0.1", port=int(os.environ.get("JOBPILOT_MCP_PORT", "8766")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
