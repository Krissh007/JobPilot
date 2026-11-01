"""
JobPilot API (FastAPI). Localhost only:

    .\\.venv\\Scripts\\uvicorn jobpilot.api.main:app --host 127.0.0.1 --port 8000

Every route lives under /api. The React dashboard (Phase 5) talks to it. If the dashboard
has been built (`npm run build` in web/), it is served from / too, so one server is enough:
open http://127.0.0.1:8000. `npm run dev` (port 5173) is only needed while changing the UI.

Who may call it (Host check, dashboard header or bearer token): see jobpilot/api/security.py.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from jobpilot.api.routers import analytics, answers, applications, data, jobs, runs, schedule, settings
from jobpilot.api.security import DEV_ORIGINS, RequestGuard, load_api_token, usable_token
from jobpilot.config import ROOT

WEB_DIST = ROOT / "web" / "dist"
# Windows takes MIME types from the registry, which on some PCs maps .js to text/plain;
# browsers then refuse the dashboard's module scripts. Pin the types the build uses.
for _type, _ext in (("text/javascript", ".js"), ("text/css", ".css"), ("image/svg+xml", ".svg"), ("application/json", ".json")):
    mimetypes.add_type(_type, _ext)
DEV_ENV = "JOBPILOT_DEV"   # "1": also accept the Vite dev server's origin (npm run dev)


def create_app(db_url: str | None = None, web_dist: Path | None = WEB_DIST, *, api_token: str | None = None,
               test_mode: bool = False, dev_mode: bool | None = None) -> FastAPI:
    """api_token: the agents' bearer token (None or under 32 characters turns token auth off). test_mode: also accept the
    "testserver" Host used by Starlette's TestClient. dev_mode: default from JOBPILOT_DEV."""
    dev = os.environ.get(DEV_ENV) == "1" if dev_mode is None else dev_mode
    app = FastAPI(title="JobPilot", version="0.4.0")
    app.state.db_url = db_url
    app.state.processes = {}  # run_id -> Popen, for agents started from the dashboard

    app.add_middleware(CORSMiddleware, allow_origins=list(DEV_ORIGINS) if dev else [], allow_methods=["*"], allow_headers=["*"])
    # Added last, so it is the outermost layer: a bad Host is refused before CORS or any route sees the request.
    app.add_middleware(RequestGuard, token=usable_token(api_token, warn=not test_mode),
                       test_mode=test_mode, dev_mode=dev)
    for router in (data.router, jobs.router, applications.router, runs.router, answers.router,
                   settings.router, schedule.router, analytics.router):
        app.include_router(router, prefix="/api")
    if web_dist is not None and (web_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="dashboard")  # after /api, so it never shadows it
    return app


app = create_app(api_token=load_api_token(ROOT / ".env"))
