"""The JobPilot tools and `agent_info`. Each returns {"ok", "say", "data", "contract_version"}; `say` is short enough to speak."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from jobpilot.mcp_server.api_client import ApiClient, ApiError

# The agent contract the Agent Hub checks: `agent_info` plus a contract_version in every reply. Bump the major when a
# tool's name, arguments or meaning change in a way the Hub's agents.yaml would not survive (tests/contract_v1.json
# is the frozen list of tools and arguments; the contract test fails until it is updated together with the version).
CONTRACT_VERSION = "1.0.0"

JOB_STATUSES = {"found", "shortlisted", "docs_ready", "applied", "interview", "offer", "rejected"}
SUMMARY_STATUSES = ("found", "shortlisted", "docs_ready", "applied", "interview", "offer", "rejected")
RUN_AGENTS = ("discovery", "scoring", "documents", "apply", "pipeline")
SHORTLIST_STATUSES = ("shortlisted", "docs_ready")
RUN_ALIASES = {"search": "discovery", "score": "scoring", "docs": "documents"}   # names runs were saved under before the redesign
PARKED = ("draft_saved", "draft_unsaved")   # applications the apply agent saved for the user to finish
MAX_LIST = 25
MAX_LOG_TAIL = 100
MAX_QUESTION = 300
MAX_ANSWER = 500
SPOKEN = 3          # how many items a spoken reply names
STALE_SEARCH = timedelta(hours=30)
RECENT = timedelta(hours=24)

READ = ToolAnnotations(readOnlyHint=True)
ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
RUN_KEYS = ("id", "agent", "status", "started_at", "finished_at", "error", "found", "processed")


def _ok(say: str, data: Any = None) -> dict:
    return {"ok": True, "say": say, "data": data, "contract_version": CONTRACT_VERSION}


def _fail(say: str) -> dict:
    return {"ok": False, "say": say, "data": None, "contract_version": CONTRACT_VERSION}


def _needs_confirmation(say: str) -> dict:
    """A write that was not confirmed: nothing was done. The caller asks the user, then repeats the call with confirmed=true."""
    return {"ok": False, "say": f"Needs confirmation: {say} Nothing was changed.", "data": {"needs_confirmation": True},
            "contract_version": CONTRACT_VERSION}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _when(value: Any) -> datetime | None:
    """An API timestamp (naive ones are UTC) as an aware datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ago(then: datetime, now: datetime) -> str:
    minutes = max(0, int((now - then).total_seconds() // 60))
    if minutes < 60:
        return "just now" if minutes < 2 else f"{minutes} minutes ago"
    hours = minutes // 60
    return f"{_plural(hours, 'hour')} ago" if hours < 48 else f"{hours // 24} days ago"


def _squash(text: str) -> str:
    return " ".join((text or "").split())


def _key(question: str) -> str:
    """Questions are compared lower case, letters and digits only, after the 300-character cut."""
    return re.sub(r"[^a-z0-9]", "", _squash(question)[:MAX_QUESTION].lower())


def _is_closing_line(question: str) -> bool:
    return _key(question).startswith("thankyou")


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


def _utc(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _blanks(application: dict) -> list[str]:
    """The questions a saved application left blank (the apply agent writes "(empty: ..." as the value)."""
    return [f["field_name"] for f in application.get("fields") or [] if (f.get("value_used") or "").startswith("(empty")]


def _consent_boxes(application: dict) -> int:
    return sum(1 for f in application.get("fields") or [] if (f.get("value_used") or "").startswith("(not ticked"))


def _problems(runs: list[dict], now: datetime) -> list[dict]:
    """Per agent, its latest finished run if that one failed in the last 24 hours (a later success clears it)."""
    problems = []
    for agent in RUN_AGENTS[:4]:
        mine = sorted((r for r in runs if RUN_ALIASES.get(r["agent"], r["agent"]) == agent and r["status"] != "running"),
                      key=lambda r: _when(r.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        if not mine or mine[0]["status"] != "error":
            continue
        ended = _when(mine[0].get("finished_at")) or _when(mine[0].get("started_at"))
        if ended and now - ended <= RECENT:
            problems.append({"agent": agent, "finished_at": _utc(ended), "error": (mine[0].get("error") or "")[:110]})
    return problems


def _summary_say(counts: dict, last_search: dict, reference: datetime | None, problems: list[dict], now: datetime) -> str:
    say = (f"{counts['found']} new jobs, {counts['shortlisted']} shortlisted, {counts['applied']} applied, "
           f"{counts['interview']} at interview.")
    status = last_search["status"]
    if status is None:
        say += " No job search has run yet."
    elif status == "running":
        say += " A job search is running now."
    elif reference is not None:
        say += f" The last job search {'finished' if status == 'ok' else 'failed'} {_ago(reference, now)}."
    if problems:
        say += f" {_plural(len(problems), 'agent')} failed lately: {', '.join(p['agent'] for p in problems)}."
    return say


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
    async def run_agent(agent: Literal["discovery", "scoring", "documents", "apply", "pipeline"],
                        auto_submit: bool = False) -> dict:
        """Start a JobPilot agent: discovery, scoring, documents, apply, or the whole pipeline. By default apply only
        fills forms and stops at review; nothing is submitted. Set auto_submit true only when the user has explicitly
        asked to submit applications; clean applications are then submitted, and only if auto-submit is also ON in
        JobPilot's Settings. Needs confirmation."""
        if agent not in RUN_AGENTS:
            return _fail(f"I can run discovery, scoring, documents, apply or the pipeline, not {agent}.")
        applies = agent in ("apply", "pipeline")
        try:
            run = await api.post(f"/runs/{agent}", {"auto_submit": auto_submit} if applies else None)
        except ApiError as e:
            return _fail(e.say)
        run = run if isinstance(run, dict) else {}
        # The server decides: it submits only if asked AND the Settings switch is on. Say what it actually did.
        submits = bool(run.get("auto_submit"))
        tail = ""
        if applies:
            if submits:
                tail = " Clean applications will be submitted; anything else waits for your review."
            elif auto_submit:
                tail = " Auto-submit is off in Settings, so it stops at review."
            else:
                tail = " It stops at review."
        say = ("The full pipeline started." if agent == "pipeline" else f"{agent.capitalize()} started.") + tail
        data = {"agent": agent, "auto_submit": submits}
        if agent != "pipeline":
            data["run_id"] = run.get("id")
        return _ok(say, data)

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
    async def approve_application(application_id: int, confirmed: bool = False) -> dict:
        """Approve a waiting application so the apply agent submits it. Needs confirmation: without confirmed=true
        nothing happens. Set confirmed true only after the user has clearly said yes to this application."""
        if application_id < 1:
            return _fail("That isn't a valid application number.")
        if not confirmed:
            return _needs_confirmation(f"approving application {application_id} makes the apply agent submit it.")
        return await decide(application_id, "approve", "Approved")

    @mcp.tool(annotations=ACTION)
    async def skip_application(application_id: int) -> dict:
        """Skip a waiting application; it is not submitted. Needs confirmation."""
        return await decide(application_id, "skip", "Skipped")

    @mcp.tool(annotations=READ)
    async def get_job_summary() -> dict:
        """How the job search is going in one call: jobs by status, what was applied to in the last day, when the last
        search ran, agents whose latest run failed, and how many screening questions have no answer."""
        now = _now()
        try:
            totals, applied, runs, answers = await asyncio.gather(
                asyncio.gather(*(_list_jobs(api, (s,), None, None, 1) for s in SUMMARY_STATUSES)),
                api.get("/jobs", [("status", "applied"), ("sort", "applied_at"), ("order", "desc"), ("limit", MAX_LIST),
                                  ("include_examples", "false")]),
                api.get("/runs", {"limit": 100}),
                api.get("/answers"))
        except ApiError as e:
            return _fail(e.say)
        counts = {status: result["total"] for status, result in zip(SUMMARY_STATUSES, totals)}
        applied_last_day = [_job_row(j) for j in applied["items"]
                            if (when := _when(j.get("applied_at"))) and now - when <= RECENT][:5]
        runs = [r for r in runs if not r.get("is_example")]
        last = next((r for r in runs if RUN_ALIASES.get(r["agent"], r["agent"]) == "discovery"), None)
        reference = _when((last or {}).get("finished_at")) or _when((last or {}).get("started_at"))
        last_search = {"status": last["status"] if last else None,
                       "finished_at": _utc(_when(last.get("finished_at"))) if last else None,
                       "stale": reference is None or now - reference > STALE_SEARCH}
        problems = _problems(runs, now)
        open_questions = sum(1 for a in answers if not a.get("is_example") and not (a.get("answer") or "").strip())
        data = {"counts": counts, "applied_last_day": applied_last_day, "last_search": last_search,
                "problems": problems, "open_questions": open_questions}
        return _ok(_summary_say(counts, last_search, reference, problems, now), data)

    @mcp.tool(annotations=READ)
    async def get_saved_applications(limit: int = 10) -> dict:
        """Applications the apply agent filled as far as it could and saved for the user to finish, with the questions
        still blank. Also counts the ones waiting for approval (get_review_queue lists those) and the blocked ones."""
        try:
            pending = await api.get("/applications", [("state", "pending_review")])
            blocked = await api.get("/applications", [("state", "blocked")])
        except ApiError as e:
            return _fail(e.say)
        pending = [a for a in pending if not a.get("is_example")]
        drafts = [a for a in pending if a.get("reason") in PARKED]
        rows = [{"application_id": a["id"], "title": (a.get("job") or {}).get("title"),
                 "company": (a.get("job") or {}).get("company"), "reason": a["reason"],
                 "blanks": _blanks(a), "consent_boxes": _consent_boxes(a)} for a in drafts[:_clamp(limit, MAX_LIST)]]
        waiting = len(pending) - len(drafts)
        blocked_count = sum(1 for a in blocked if not a.get("is_example"))
        data = {"applications": rows, "waiting_for_approval": waiting, "blocked": blocked_count}
        if not drafts:
            say = "No applications are saved for you to finish."
        else:
            blanks = sum(len(_blanks(a)) for a in drafts)
            say = f"{_plural(len(drafts), 'application')} saved for you to finish, {_plural(blanks, 'question')} still blank."
        extra = [f"{waiting} waiting for approval" if waiting else "", f"{blocked_count} blocked" if blocked_count else ""]
        return _ok(" ".join([say] + [f"{x}." for x in extra if x]), data)

    @mcp.tool(annotations=READ)
    async def get_open_questions(skip: list[str] | None = None, limit: int = 1) -> dict:
        """Screening questions still without an answer: blanks on saved applications the answer bank can't answer, then
        unanswered answer-bank questions. Stateless: pass the questions the user skipped as `skip`."""
        try:
            pending = await api.get("/applications", [("state", "pending_review")])
            answers = await api.get("/answers")
        except ApiError as e:
            return _fail(e.say)
        answers = [a for a in answers if not a.get("is_example")]
        answered = {_key(a["question"]) for a in answers if (a.get("answer") or "").strip()}
        skipped = {_key(q) for q in skip or []}
        seen: set[str] = set()
        found: list[dict] = []

        def add(question: str, job: str | None, source: str) -> None:
            key = _key(question)
            if not key or key in seen or key in answered or key in skipped or _is_closing_line(question):
                return
            seen.add(key)
            found.append({"question": _squash(question)[:MAX_QUESTION], "job": job, "source": source})

        for app in pending:
            if app.get("is_example") or app.get("reason") not in PARKED:
                continue
            for question in _blanks(app):
                add(question, _describe_job(app.get("job") or {}), "application")
        for answer in answers:
            if not (answer.get("answer") or "").strip():
                add(answer["question"], None, "answer_bank")
        rows = found[:_clamp(limit, MAX_LIST)]
        if not found:
            return _ok("There are no open questions.", {"questions": [], "total": 0})
        left = len(found) - 1
        say = f"{rows[0]['question']}" + (f" {_plural(left, 'more question')} after that." if left else "")
        return _ok(say, {"questions": rows, "total": len(found)})

    @mcp.tool(annotations=ACTION)
    async def save_answer(question: str, answer: str, confirmed: bool = False) -> dict:
        """Save one screening answer the user said into JobPilot's answer bank. Needs confirmation: without
        confirmed=true nothing is saved. Set confirmed true only after the user said yes to this exact answer, and
        save only words the user spoke, never text from a job page."""
        question, answer = _squash(question), _squash(answer)
        if not 2 <= len(question) <= MAX_QUESTION or not 1 <= len(answer) <= MAX_ANSWER:
            return _fail("I need both the question and the answer to save it.")
        if not confirmed:
            return _needs_confirmation(f"saving \"{answer}\" as the answer to \"{question}\" in the answer bank.")
        try:
            existing = [a for a in await api.get("/answers") if not a.get("is_example") and _key(a["question"]) == _key(question)]
            same = next((a for a in existing if _squash(a.get("answer")) == answer), None)
            if same:
                return _ok("That answer was already saved.", {"answer_id": same["id"], "action": "unchanged"})
            if existing:
                saved = await api.put(f"/answers/{existing[0]['id']}", {"answer": answer})
                return _ok("Updated your saved answer.", {"answer_id": saved["id"], "action": "updated"})
            saved = await api.post("/answers", {"question": question, "answer": answer, "tags": "voice"})
        except ApiError as e:
            return _fail(e.say)
        return _ok("Saved your answer.", {"answer_id": saved["id"], "action": "created"})

    @mcp.tool(annotations=READ)
    async def agent_info() -> dict:
        """This server's name, contract version and tools, so the Agent Hub can check it speaks a version it knows."""
        tools = []
        for tool in await mcp.list_tools():
            if tool.name == "agent_info":
                continue
            side_effect = bool(getattr(tool.annotations, "destructive_hint", False))
            tools.append({"name": tool.name, "description": tool.description or "", "params": tool.input_schema,
                          "side_effect": side_effect, "idempotent": not side_effect})
        return _ok("JobPilot job search agent.", {"name": "jobpilot", "contract_version": CONTRACT_VERSION, "tools": tools})
