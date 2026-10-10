"""
JobPilot API (FastAPI). Localhost only:

    .\\.venv\\Scripts\\uvicorn jobpilot.api.main:app --host 127.0.0.1 --port 8000

Every route lives under /api. The React dashboard (Phase 5) talks to it. If the dashboard
has been built (`npm run build` in web/), it is served from / too, so one server is enough:
open http://127.0.0.1:8000. `npm run dev` (port 5173) is only needed while changing the UI.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from jobpilot.api.routers import analytics, answers, applications, data, jobs, runs, schedule, settings
from jobpilot.config import ROOT

WEB_DIST = ROOT / "web" / "dist"
# Windows takes MIME types from the registry, which on some PCs maps .js to text/plain;
# browsers then refuse the dashboard's module scripts. Pin the types the build uses.
for _type, _ext in (("text/javascript", ".js"), ("text/css", ".css"), ("image/svg+xml", ".svg"), ("application/json", ".json")):
    mimetypes.add_type(_type, _ext)
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


def create_app(db_url: str | None = None, web_dist: Path | None = WEB_DIST) -> FastAPI:
    app = FastAPI(title="JobPilot", version="0.4.0")
    app.state.db_url = db_url
    app.state.processes = {}  # run_id -> Popen, for agents started from the dashboard

    @app.middleware("http")
    async def localhost_only(request: Request, call_next):
        # Defence in depth: uvicorn is bound to 127.0.0.1, but never serve another machine.
        host = request.client.host if request.client else ""
        if host not in LOOPBACK:
            return JSONResponse({"detail": "JobPilot only serves this computer"}, status_code=403)
        return await call_next(request)

    app.add_middleware(CORSMiddleware, allow_origins=DEV_ORIGINS, allow_methods=["*"], allow_headers=["*"])
    for router in (data.router, jobs.router, applications.router, runs.router, answers.router,
                   settings.router, schedule.router, analytics.router):
        app.include_router(router, prefix="/api")
    if web_dist is not None and (web_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="dashboard")  # after /api, so it never shadows it
    return app


app = create_app()
