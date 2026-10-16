"""The seven JobPilot tools. Each returns {"ok", "say", "data"}; `say` is short enough to speak."""
from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jobpilot.mcp_server.api_client import ApiClient, ApiError

JOB_STATUSES = {"found", "shortlisted", "docs_ready", "applied", "interview", "offer", "rejected"}
RUN_AGENTS = ("discovery", "scoring", "documents", "apply", "pipeline")
SHORTLIST_STATUSES = ("shortlisted", "docs_ready")
MAX_LIST = 25
MAX_LOG_TAIL = 100
SPOKEN = 3          # how many items a spoken reply names

READ = ToolAnnotations(readOnlyHint=True)
ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
RUN_KEYS = ("id", "agent", "status", "started_at", "finished_at", "error")


def _ok(say: str, data: Any = None) -> dict:
    return {"ok": True, "say": say, "data": data}


def _fail(say: str) -> dict:
    return {"ok": False, "say": say, "data": None}


def _job_row(job: dict) -> dict:
    return {k: job.get(k) for k in ("id", "title", "company", "score", "ai_tier", "status", "url")}


def _clamp(value: int, high: int) -> int:
    return max(1, min(int(value), high))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _describe_job(job: dict) -> str:
    return f"{job.get('title') or 'a job'} at {job.get('company') or 'an unknown company'}"


def _names(rows: list[dict]) -> str:
    return "; ".join(_describe_job(r) for r in rows[:SPOKEN])


async def _list_jobs(api: ApiClient, statuses: tuple[str, ...], min_score: float | None, query: str | None,
                     limit: int) -> dict:
    params: list[tuple[str, Any]] = [("status", s) for s in statuses]
    params += [("sort", "score"), ("order", "desc"), ("limit", _clamp(limit, MAX_LIST)), ("include_examples", "false")]
    if min_score is not None:
        params.append(("min_score", min_score))
    if query and query.strip():
        params.append(("q", query.strip()))
    return await api.get("/jobs", params)


def register(mcp: MCPServer, api: ApiClient) -> None:
    @mcp.tool(annotations=READ)
    async def get_jobs(status: str | None = None, min_score: float | None = None, query: str | None = None,
                       limit: int = 10) -> dict:
        """List jobs, best score first. Optional filters: status (found, shortlisted, docs_ready, applied,
        interview, offer, rejected), minimum score, and a text query. Use for general questions about jobs."""
        if status and status not in JOB_STATUSES:
            return _fail(f"I don't know the job status {status}. Use one of {', '.join(sorted(JOB_STATUSES))}.")
        try:
            result = await _list_jobs(api, (status,) if status else (), min_score, query, limit)
        except ApiError as e:
            return _fail(e.say)
        rows = [_job_row(j) for j in result["items"]]
        total = result["total"]
        label = f"{status.replace('_', ' ')} " if status else ""
        if not rows:
            return _ok(f"No {label}jobs found.", {"total": total, "jobs": []})
        return _ok(f"{_plural(total, label + 'job')}. Top: {_names(rows)}.", {"total": total, "jobs": rows})

    @mcp.tool(annotations=READ)
    async def get_shortlist(limit: int = 10) -> dict:
        """The shortlist: jobs that scored well and are shortlisted or have their documents ready.
        Use for "what's on my shortlist"."""
        try:
            result = await _list_jobs(api, SHORTLIST_STATUSES, None, None, limit)
        except ApiError as e:
            return _fail(e.say)
        rows = [_job_row(j) for j in result["items"]]
        total = result["total"]
        if not rows:
            return _ok("Your shortlist is empty.", {"total": 0, "jobs": []})
        return _ok(f"You have {total} on your shortlist. Top: {_names(rows)}.", {"total": total, "jobs": rows})

    @mcp.tool(annotations=READ)
    async def get_review_queue() -> dict:
        """Applications filled in and waiting for the user to approve or skip. Use for "approvals waiting"."""
        try:
            apps = await api.get("/applications", [("state", "pending_review")])
        except ApiError as e:
            return _fail(e.say)
        rows = [{"application_id": a["id"], "title": (a.get("job") or {}).get("title"),
                 "company": (a.get("job") or {}).get("company"), "needs_review": a.get("needs_review", 0),
                 "reason": a.get("reason", "")} for a in apps if not a.get("is_example")]
        if not rows:
            return _ok("Nothing is waiting for approval.", {"count": 0, "applications": []})
        spoken = "; ".join(f"application {r['application_id']}, {_describe_job(r)}" for r in rows[:SPOKEN])
        say = f"{_plural(len(rows), 'application')} waiting for approval: {spoken}."
        return _ok(say, {"count": len(rows), "applications": rows})

    @mcp.tool(annotations=READ)
    async def get_run_logs(agent: str | None = None, run_id: int | None = None, tail: int = 30) -> dict:
        """Agent runs. With no run_id: the most recent runs and how they ended (optionally for one agent:
        discovery, scoring, documents, apply). With run_id: the last lines of that run's log."""
        if run_id is not None:
            try:
                run = await api.get(f"/runs/{int(run_id)}")
            except ApiError as e:
                return _fail(e.say)
            lines = (run.get("log") or "").splitlines()[-_clamp(tail, MAX_LOG_TAIL):]
            say = f"Run {run['id']}, {run['agent']}, is {run['status']}."
            if run.get("error"):
                say += f" Error: {run['error']}"
            return _ok(say, {"run": {k: run.get(k) for k in RUN_KEYS}, "log": lines})
        if agent and agent not in RUN_AGENTS[:4]:
            return _fail(f"I don't know the agent {agent}. Use discovery, scoring, documents or apply.")
        try:
            runs = await api.get("/runs", {"limit": 5, **({"agent": agent} if agent else {})})
        except ApiError as e:
            return _fail(e.say)
        rows = [{k: r.get(k) for k in RUN_KEYS} for r in runs if not r.get("is_example")]
        if not rows:
            return _ok("No runs yet.", {"runs": []})
        latest = rows[0]
        say = f"The latest run was {latest['agent']}, {latest['status']}."
        if latest.get("error"):
            say += f" Error: {latest['error']}"
        return _ok(say, {"runs": rows})

    @mcp.tool(annotations=ACTION)
    async def run_agent(agent: Literal["discovery", "scoring", "documents", "apply", "pipeline"]) -> dict:
        """Start a JobPilot agent: discovery, scoring, documents, apply, or the whole pipeline. Apply only
        fills forms for review and never submits. Needs confirmation."""
        if agent not in RUN_AGENTS:
            return _fail(f"I can run discovery, scoring, documents, apply or the pipeline, not {agent}.")
        try:
            if agent == "pipeline":
                await api.post("/runs/pipeline")
                return _ok("The full pipeline started. Apply stops at review.", {"agent": agent})
            run = await api.post(f"/runs/{agent}", {"review_only": True} if agent == "apply" else None)
        except ApiError as e:
            return _fail(e.say)
        say = f"{agent.capitalize()} started." + (" It will stop at review." if agent == "apply" else "")
        return _ok(say, {"agent": agent, "run_id": run.get("id")})

    async def decide(application_id: int, verb: str, past: str) -> dict:
        if application_id < 1:
            return _fail("That isn't a valid application number.")
        try:
            app = await api.post(f"/applications/{application_id}/{verb}")
        except ApiError as e:
            return _fail(e.say)
        return _ok(f"{past} application {application_id}, {_describe_job(app.get('job') or {})}.",
                   {"application_id": application_id, "state": app.get("state")})

    @mcp.tool(annotations=ACTION)
    async def approve_application(application_id: int) -> dict:
        """Approve a waiting application so the apply agent submits it. Needs confirmation."""
        return await decide(application_id, "approve", "Approved")

    @mcp.tool(annotations=ACTION)
    async def skip_application(application_id: int) -> dict:
        """Skip a waiting application; it is not submitted. Needs confirmation."""
        return await decide(application_id, "skip", "Skipped")
