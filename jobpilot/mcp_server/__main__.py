"""python -m jobpilot.mcp_server  (127.0.0.1:8766/mcp; needs JOBPILOT_MCP_TOKEN and JOBPILOT_API_TOKEN)."""
from __future__ import annotations

import os
import sys

import uvicorn
from dotenv import load_dotenv

from jobpilot.config import ROOT
from jobpilot.mcp_server.api_client import DEFAULT_API_URL, ApiClient
from jobpilot.mcp_server.server import create_app

TOKEN_ENV = "JOBPILOT_API_TOKEN"   # the bearer JobPilot's API expects from agents (same name as jobpilot/api/security.py)
MIN_TOKEN_LENGTH = 32


def main() -> int:
    load_dotenv(ROOT / ".env")
    token = os.environ.get("JOBPILOT_MCP_TOKEN", "")
    if len(token) < 16:
        print("Set JOBPILOT_MCP_TOKEN (at least 16 characters) in .env or the environment.", file=sys.stderr)
        return 2
    api_token = os.environ.get(TOKEN_ENV, "").strip()
    if len(api_token) < MIN_TOKEN_LENGTH:   # without it every state-changing call would be refused by JobPilot's API
        print(f"Set {TOKEN_ENV} (at least {MIN_TOKEN_LENGTH} characters) in .env or the environment; "
              "it must match the value the JobPilot API runs with.", file=sys.stderr)
        return 2
    api = ApiClient(os.environ.get("JOBPILOT_API_URL", DEFAULT_API_URL), api_token=api_token)
    uvicorn.run(create_app(api, token), host="127.0.0.1", port=int(os.environ.get("JOBPILOT_MCP_PORT", "8766")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
