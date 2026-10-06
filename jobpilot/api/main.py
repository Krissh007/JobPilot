"""
JobPilot API (FastAPI). Localhost only:

    .\\.venv\\Scripts\\uvicorn jobpilot.api.main:app --host 127.0.0.1 --port 8000

Every route lives under /api. The React dashboard (Phase 5) talks to it.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from jobpilot.api.routers import analytics, answers, applications, data, jobs, runs, settings

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


def create_app(db_url: str | None = None) -> FastAPI:
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
                   settings.router, analytics.router):
        app.include_router(router, prefix="/api")
    return app


app = create_app()
