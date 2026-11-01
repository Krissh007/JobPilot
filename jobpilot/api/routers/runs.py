"""
Agent runs: history, "Run now" (spawns the agent as a subprocess) and a live
log stream over Server-Sent Events.

The stream tails agent_runs.log in the database rather than the process's
pipe, so it works the same for runs started by Task Scheduler.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from jobpilot.api.deps import get_or_404, get_session
from jobpilot.api.security import TOKEN_ENV
from jobpilot.config import ROOT
from jobpilot.db.models import AgentName, AgentRun, RunStatus, utcnow
from jobpilot.db.repo import get_setting
from jobpilot.db.session import get_engine

router = APIRouter(tags=["runs"])

# The four agents and the module each one runs. The retired search/score/docs names are history only.
MODULES = {AgentName.discovery: "discover", AgentName.scoring: "shortlist", AgentName.documents: "documents",
           AgentName.apply: "apply"}
RUNNABLE = set(MODULES)
COMING: dict[AgentName, str] = {}
STALE_AFTER = timedelta(hours=2)  # a "running" row this old with no live process is treated as dead
LOG_DIR = ROOT / "data" / "logs"


class RunOptions(BaseModel):
    """Whitelisted options only; nothing from the request reaches a shell."""
    limit: Optional[int] = Field(None, ge=1, le=500)  # scoring, documents, apply
    job_id: Optional[int] = Field(None, ge=1)          # scoring, documents, apply
    auto_submit: bool = False                          # apply: submit clean forms, but only if the auto_submit setting is ON too
    review_only: bool = False                          # apply: veto; wins over auto_submit (older callers)


class PipelineOptions(BaseModel):
    auto_submit: bool = False                          # same meaning as RunOptions.auto_submit, for the apply step


def submits(requested: bool, vetoed: bool, session: Session) -> bool:
    """A run submits only when the caller asked for it AND the auto_submit setting is ON. Anything else is review-only."""
    return requested and not vetoed and bool(get_setting(session, "auto_submit"))


def mode_line(requested: bool, setting_on: bool, effective: bool) -> str:
    if effective:
        return "Auto-submit ON (requested in the run request, and on in Settings)"
    if requested and not setting_on:
        return "Review-only (auto-submit was requested but is OFF in Settings)"
    return "Review-only (no auto-submit requested)"


def build_command(agent: AgentName, run_id: int, options: RunOptions, auto_submit: bool = False) -> list[str]:
    command = [sys.executable, "-m", f"jobpilot.agents.{MODULES[agent]}", "--run-id", str(run_id)]
    if agent == AgentName.apply:
        command += (["--job-id", str(options.job_id)] if options.job_id else []) + ["--review-only"] * (not auto_submit)
        command += ["--limit", str(options.limit)] if options.limit else []
    elif agent in (AgentName.scoring, AgentName.documents):
        if options.limit:
            command += ["--limit", str(options.limit)]
        if options.job_id:
            command += ["--job-id", str(options.job_id)]
    return command


def child_env(request: Request, caller: str | None = None, requested: bool | None = None,
              setting: bool | None = None) -> dict[str, str]:
    """The environment an agent process starts with. The API token stays out of it; the run's origin goes in."""
    env = {k: v for k, v in os.environ.items() if k != TOKEN_ENV}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
    if request.app.state.db_url:
        env["DATABASE_URL"] = request.app.state.db_url
    for name, value in (("JOBPILOT_RUN_CALLER", caller), ("JOBPILOT_RUN_AUTO_SUBMIT_REQUESTED", requested),
                        ("JOBPILOT_RUN_AUTO_SUBMIT_SETTING", setting)):
        if value is not None:
            env[name] = value if isinstance(value, str) else str(int(value))
    return env


def caller_of(request: Request) -> str | None:
    return getattr(request.state, "caller", None)


def reap(request: Request, session: Session) -> None:
    """Mark runs whose process died without recording an outcome (e.g. an import error)."""
    processes: dict[int, subprocess.Popen] = request.app.state.processes
    for run_id, process in list(processes.items()):
        code = process.poll()
        if code is None:
            continue
        processes.pop(run_id)
        run = session.get(AgentRun, run_id)
        if run is not None and run.status == RunStatus.running:
            run.status, run.finished_at = RunStatus.error, utcnow()
            run.error = f"Agent process exited with code {code}; see data/logs/run_{run_id}.log"
            session.add(run)
    session.commit()


def serialize(run: AgentRun, with_log: bool = False) -> dict[str, Any]:
    data = run.model_dump(exclude={"log"})
    data["log_lines"] = run.log.count("\n") + 1 if run.log else 0
    if with_log:
        data["log"] = run.log
    return data


@router.get("/runs")
def list_runs(request: Request, agent: Optional[AgentName] = None, limit: int = 50,
              session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    reap(request, session)
    query = select(AgentRun).order_by(AgentRun.started_at.desc()).limit(min(limit, 500))
    if agent:
        query = query.where(AgentRun.agent == agent)
    return [serialize(run) for run in session.exec(query)]


@router.get("/runs/{run_id}")
def get_run(run_id: int, request: Request, session: Session = Depends(get_session)) -> dict[str, Any]:
    reap(request, session)
    return serialize(get_or_404(session, AgentRun, run_id), with_log=True)


@router.post("/runs/pipeline", status_code=202)
def run_now(request: Request, options: PipelineOptions = PipelineOptions(),
            session: Session = Depends(get_session)) -> dict[str, Any]:
    """ Run now: Discovery, Scoring, Documents, then Apply, one after another. Apply stops at review unless the body
    says {"auto_submit": true} AND the auto_submit setting is ON; then clean forms are submitted and the rest wait in Review.
    Each agent records its own agent_runs row, so the Runs screen shows them as they start."""
    reap(request, session)
    busy = session.exec(select(AgentRun).where(AgentRun.agent.in_(list(RUNNABLE)), AgentRun.status == RunStatus.running,
                                               AgentRun.is_example.is_(False))).all()
    live = [r for r in busy if r.id in request.app.state.processes or utcnow() - _aware(r.started_at) < STALE_AFTER]
    if live:
        raise HTTPException(409, {"message": f"A {live[0].agent.value} run is already in progress", "run_id": live[0].id})
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    setting_on = bool(get_setting(session, "auto_submit"))
    effective = submits(options.auto_submit, False, session)
    env = child_env(request, caller_of(request), options.auto_submit, setting_on)
    command = [sys.executable, "-m", "jobpilot.scheduled", "--now", "--auto-submit" if effective else "--review-only"]
    with open(LOG_DIR / "run_now.log", "w", encoding="utf-8") as console:
        subprocess.Popen(command, cwd=ROOT, env=env, stdout=console, stderr=subprocess.STDOUT,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"started": [a.value for a in MODULES], "log": "data/logs/run_now.log", "auto_submit": effective,
            "mode": mode_line(options.auto_submit, setting_on, effective)}


@router.post("/runs/{agent}", status_code=202)
def start_run(agent: AgentName, request: Request, options: RunOptions = RunOptions(),
              session: Session = Depends(get_session)) -> dict[str, Any]:
    if agent not in RUNNABLE:
        raise HTTPException(400, f"The {agent.value} agent is retired; use Discovery, Scoring, Documents or Apply")
    if agent in COMING:
        raise HTTPException(501, f"The {agent.value} agent arrives in {COMING[agent]}")
    reap(request, session)
    busy = session.exec(select(AgentRun).where(AgentRun.agent == agent, AgentRun.status == RunStatus.running,
                                               AgentRun.is_example.is_(False))).all()
    live = [r for r in busy if r.id in request.app.state.processes or utcnow() - _aware(r.started_at) < STALE_AFTER]
    if live:
        raise HTTPException(409, {"message": f"A {agent.value} run is already in progress", "run_id": live[0].id})

    effective = agent == AgentName.apply and submits(options.auto_submit, options.review_only, session)
    setting_on = bool(get_setting(session, "auto_submit"))
    requested = options.auto_submit and not options.review_only
    mode = mode_line(requested, setting_on, effective) if agent == AgentName.apply else None
    caller = caller_of(request)
    run = AgentRun(agent=agent, status=RunStatus.running, caller=caller,
                   log="Starting..." + (f" {mode} (started by {caller or 'unknown'})" if mode else ""))
    if agent == AgentName.apply:
        run.auto_submit_requested, run.auto_submit_setting = requested, setting_on
    session.add(run)
    session.commit()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = child_env(request)
    with open(LOG_DIR / f"run_{run.id}.log", "w", encoding="utf-8") as console:
        process = subprocess.Popen(
            build_command(agent, run.id, options, effective), cwd=ROOT, env=env, stdout=console, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    request.app.state.processes[run.id] = process
    return {**serialize(run), "auto_submit": effective}


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _snapshot(db_url: str | None, run_id: int) -> tuple[str, str, str] | None:
    with Session(get_engine(db_url)) as session:
        run = session.get(AgentRun, run_id)
        return (run.log, run.status.value, run.error) if run else None


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: int, request: Request) -> StreamingResponse:
    """SSE: one `log` event per new log line, then `end` with the outcome."""
    db_url = request.app.state.db_url
    poll = getattr(request.app.state, "sse_poll_seconds", 1.0)
    if _snapshot(db_url, run_id) is None:
        raise HTTPException(404, f"run {run_id} not found")

    async def events():
        sent, idle = 0, 0.0
        while True:
            if await request.is_disconnected():
                return
            log, status, error = await asyncio.to_thread(_snapshot, db_url, run_id)
            lines = log.splitlines() if log else []
            for line in lines[sent:]:
                yield f"event: log\ndata: {json.dumps(line)}\n\n"
                idle = 0.0
            sent = max(sent, len(lines))
            if status != RunStatus.running.value:
                yield f"event: end\ndata: {json.dumps({'status': status, 'error': error})}\n\n"
                return
            idle += poll
            if idle >= 15:
                yield ": keep-alive\n\n"
                idle = 0.0
            await asyncio.sleep(poll)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
