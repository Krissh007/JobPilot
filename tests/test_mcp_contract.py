"""The MCP contract (version 1.0.0): the version in every reply, agent_info, the four newer tools and the frozen tool list."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from mcp.client.client import Client

from jobpilot.mcp_server import tools as tools_module
from jobpilot.mcp_server.api_client import ApiClient
from jobpilot.mcp_server.server import create_mcp_server
from jobpilot.mcp_server.tools import CONTRACT_VERSION
from tests.test_mcp_server import FakeApi, call

pytestmark = pytest.mark.anyio
SNAPSHOT = Path(__file__).with_name("contract_v1.json")
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
ALL_TOOLS = ["get_jobs", "get_shortlist", "get_review_queue", "get_run_logs", "run_agent", "approve_application",
             "skip_application", "get_job_summary", "get_saved_applications", "get_open_questions", "save_answer"]
WRITES = {"run_agent", "approve_application", "skip_application", "save_answer"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(tools_module, "_now", lambda: NOW)


class Routes(FakeApi):
    """FakeApi where a route may also be a function of the request."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.routes.get((request.method, request.url.path))
        if callable(reply):
            reply = reply(request)
        if isinstance(reply, Exception):
            raise reply
        status, body = reply if reply else (404, {"detail": "Not found"})
        return httpx.Response(status, json=body)

    @property
    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET"]


def make(routes: dict | None = None) -> tuple[Routes, Client]:
    fake = Routes(routes)
    return fake, Client(create_mcp_server(ApiClient(transport=httpx.MockTransport(fake))))


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).replace(tzinfo=None).isoformat()   # the API sends naive UTC


def contract_of(info: dict) -> dict:
    """What the Hub's agents.yaml depends on: tool names, argument names and which tools write."""
    return {"contract_version": info["contract_version"],
            "tools": {t["name"]: {"args": sorted(t["params"].get("properties", {})), "required": sorted(t["params"].get("required", [])),
                                  "side_effect": t["side_effect"]} for t in info["tools"]}}


def contract_diff(live: dict, frozen: dict) -> list[str]:
    changes = [f"tool removed or renamed: {n}" for n in sorted(frozen["tools"].keys() - live["tools"].keys())]
    changes += [f"tool added: {n}" for n in sorted(live["tools"].keys() - frozen["tools"].keys())]
    changes += [f"tool changed: {n}" for n in sorted(live["tools"].keys() & frozen["tools"].keys())
                if live["tools"][n] != frozen["tools"][n]]
    return changes


# ---- contract version ----

OK_ROUTES = {
    ("GET", "/api/jobs"): (200, {"total": 0, "items": []}),
    ("GET", "/api/applications"): (200, []),
    ("GET", "/api/runs"): (200, []),
    ("GET", "/api/answers"): (200, []),
    ("POST", "/api/runs/discovery"): (202, {"id": 3}),
    ("POST", "/api/applications/7/approve"): (200, {"id": 7, "state": "approved", "job": {}}),
    ("POST", "/api/applications/7/skip"): (200, {"id": 7, "state": "skipped", "job": {}}),
    ("POST", "/api/answers"): (201, {"id": 1}),
}
CALLS = {"get_jobs": {}, "get_shortlist": {}, "get_review_queue": {}, "get_run_logs": {}, "run_agent": {"agent": "discovery"},
         "approve_application": {"application_id": 7, "confirmed": True}, "skip_application": {"application_id": 7},
         "get_job_summary": {}, "get_saved_applications": {}, "get_open_questions": {},
         "save_answer": {"question": "Notice period?", "answer": "30 days", "confirmed": True}, "agent_info": {}}


@pytest.mark.parametrize("tool", sorted(CALLS))
async def test_every_reply_carries_the_contract_version(tool):
    _, client = make(OK_ROUTES)
    async with client:
        ok = await call(client, tool, CALLS[tool])
    assert ok["ok"] and ok["contract_version"] == CONTRACT_VERSION == "1.0.0"
    _, client = make({})                                     # every API call answers 404: the failed reply
    async with client:
        failed = await call(client, tool, CALLS[tool])
    if tool != "agent_info":                                 # agent_info never touches the API
        assert not failed["ok"]
    assert failed["contract_version"] == CONTRACT_VERSION


@pytest.mark.parametrize("tool,args", [("approve_application", {"application_id": 7}),
                                       ("save_answer", {"question": "Notice period?", "answer": "30 days"}),
                                       ("save_answer", {"question": "x", "answer": ""}),
                                       ("approve_application", {"application_id": 0, "confirmed": True})])
async def test_refused_and_invalid_replies_carry_the_version_too(tool, args):
    _, client = make(OK_ROUTES)
    async with client:
        r = await call(client, tool, args)
    assert not r["ok"] and r["contract_version"] == CONTRACT_VERSION


# ---- agent_info ----

async def test_agent_info_lists_the_eleven_tools_with_side_effect_flags():
    _, client = make()
    async with client:
        r = await call(client, "agent_info")
    assert r["ok"] and r["data"]["name"] == "jobpilot" and r["data"]["contract_version"] == r["contract_version"] == "1.0.0"
    listed = {t["name"]: t for t in r["data"]["tools"]}
    assert sorted(listed) == sorted(ALL_TOOLS)                                   # agent_info does not list itself
    for name, tool in listed.items():
        assert tool["side_effect"] is (name in WRITES) and tool["idempotent"] is (name not in WRITES)
        assert tool["description"] and tool["params"]["type"] == "object"
    assert "confirmed" in listed["save_answer"]["params"]["properties"]
    assert "confirmed" in listed["approve_application"]["params"]["properties"]


async def live_contract(server=None) -> dict:
    client = Client(server or create_mcp_server(ApiClient(transport=httpx.MockTransport(FakeApi()))))
    async with client:
        return contract_of((await call(client, "agent_info"))["data"])


async def test_tool_list_matches_the_frozen_contract():
    frozen = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    live = await live_contract()
    assert contract_diff(live, frozen) == [], "a tool changed: bump CONTRACT_VERSION and regenerate tests/contract_v1.json"
    assert frozen["contract_version"].split(".")[0] == CONTRACT_VERSION.split(".")[0]


async def test_a_renamed_tool_fails_the_contract_test():
    server = create_mcp_server(ApiClient(transport=httpx.MockTransport(FakeApi())))
    server.remove_tool("get_jobs")

    @server.tool()
    async def get_all_jobs(status: str | None = None) -> dict:
        return {"ok": True, "say": "", "data": None}

    changes = contract_diff(await live_contract(server), json.loads(SNAPSHOT.read_text(encoding="utf-8")))
    assert "tool removed or renamed: get_jobs" in changes and "tool added: get_all_jobs" in changes


async def test_a_changed_argument_or_write_flag_fails_the_contract_test():
    frozen = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    live = await live_contract()
    live["tools"]["get_jobs"] = {**live["tools"]["get_jobs"], "args": ["status"]}
    live["tools"]["skip_application"] = {**live["tools"]["skip_application"], "side_effect": False}
    assert contract_diff(live, frozen) == ["tool changed: get_jobs", "tool changed: skip_application"]


# ---- get_job_summary ----

def summary_routes(*, runs, applied=None, answers=None, totals=None):
    totals = totals or {"found": 12, "shortlisted": 4, "docs_ready": 2, "applied": 9, "interview": 1, "offer": 0, "rejected": 30}

    def jobs(request):
        params = request.url.params
        status = params.get("status")
        if params.get("sort") == "applied_at":
            return 200, {"total": totals[status], "items": applied or []}
        return 200, {"total": totals[status], "items": []}

    return {("GET", "/api/jobs"): jobs, ("GET", "/api/runs"): (200, runs), ("GET", "/api/answers"): (200, answers or [])}


def run(i, agent, status, hours_ago, error="", example=False):
    return {"id": i, "agent": agent, "status": status, "started_at": iso(hours_ago + 0.1),
            "finished_at": None if status == "running" else iso(hours_ago), "error": error, "is_example": example}


async def test_job_summary_counts_say_and_problems():
    runs = [run(9, "discovery", "ok", 3),
            run(8, "score", "error", 5, "x" * 200),                 # old name for scoring, failed, never succeeded again
            run(7, "documents", "ok", 4),
            run(6, "docs", "error", 6, "boom"),                     # old name for documents, but a later success cleared it
            run(5, "apply", "error", 30, "too old to matter"),
            run(4, "apply", "error", 1, "demo", example=True)]     # demo rows never count
    applied = [{"id": 1, "title": "ML Engineer", "company": "Acme", "score": 90, "ai_tier": "AI-core", "status": "applied",
                "url": "https://x/1", "applied_at": iso(2), "notes": "ignored"},
               {"id": 2, "title": "Old", "company": "Zeta", "applied_at": iso(30)}]
    answers = [{"id": 1, "question": "A?", "answer": ""}, {"id": 2, "question": "B?", "answer": "yes"},
               {"id": 3, "question": "Demo?", "answer": "", "is_example": True}]
    fake, client = make(summary_routes(runs=runs, applied=applied, answers=answers))
    async with client:
        r = await call(client, "get_job_summary")
    d = r["data"]
    assert r["ok"] and d["counts"] == {"found": 12, "shortlisted": 4, "docs_ready": 2, "applied": 9, "interview": 1,
                                       "offer": 0, "rejected": 30}
    assert all(("include_examples", "false") in q.url.params.multi_items() for q in fake.requests if q.url.path == "/api/jobs")
    assert [j["id"] for j in d["applied_last_day"]] == [1] and d["applied_last_day"][0]["url"] == "https://x/1"
    assert "notes" not in d["applied_last_day"][0]
    assert d["last_search"] == {"status": "ok", "finished_at": NOW.replace(hour=9).isoformat(), "stale": False}
    assert d["problems"] == [{"agent": "scoring", "finished_at": NOW.replace(hour=7).isoformat(), "error": "x" * 110}]
    assert d["open_questions"] == 1
    assert r["say"].startswith("12 new jobs, 4 shortlisted, 9 applied, 1 at interview. The last job search finished 3 hours ago.")
    assert "scoring" in r["say"]


async def test_job_summary_search_goes_stale_after_thirty_hours():
    for hours, stale in ((29, False), (31, True)):
        _, client = make(summary_routes(runs=[run(1, "search", "ok", hours)]))    # saved under the old name
        async with client:
            d = (await call(client, "get_job_summary"))["data"]
        assert d["last_search"]["status"] == "ok" and d["last_search"]["stale"] is stale


async def test_job_summary_with_no_runs_or_a_running_search():
    _, client = make(summary_routes(runs=[]))
    async with client:
        r = await call(client, "get_job_summary")
    assert r["data"]["last_search"] == {"status": None, "finished_at": None, "stale": True}
    assert r["data"]["problems"] == [] and "No job search has run yet." in r["say"]
    _, client = make(summary_routes(runs=[run(2, "discovery", "running", 0.5)]))
    async with client:
        r = await call(client, "get_job_summary")
    assert r["data"]["last_search"] == {"status": "running", "finished_at": None, "stale": False}
    assert "running now" in r["say"]


async def test_job_summary_api_down():
    _, client = make({("GET", "/api/jobs"): httpx.ConnectError("refused")})
    async with client:
        r = await call(client, "get_job_summary")
    assert not r["ok"] and "isn't running" in r["say"]


# ---- get_saved_applications ----

def saved(i, reason, fields=(), example=False, title="Role", company="Co"):
    return {"id": i, "state": "pending_review", "reason": reason, "is_example": example,
            "job": {"title": f"{title} {i}", "company": company},
            "fields": [{"field_name": n, "value_used": v} for n, v in fields]}


async def test_saved_applications_blanks_consent_and_counts():
    pending = [saved(1, "draft_saved", [("Notice period?", "(empty: needs you)"), ("Name", "Krishna"),
                                        ("I agree to the terms", "(not ticked: only you can agree to this)"),
                                        ("Salary?", "(empty: needs you)")]),
               saved(2, "draft_unsaved", [("Name", "Krishna")]),
               saved(3, "", [("Name", "Krishna")]),                       # waiting for approval, not a draft
               saved(4, "draft_saved", [("Demo?", "(empty: x)")], example=True)]
    blocked = [{"id": 5, "state": "blocked", "is_example": False}, {"id": 6, "state": "blocked", "is_example": True}]

    def apps(request):
        return 200, blocked if request.url.params.get("state") == "blocked" else pending

    fake, client = make({("GET", "/api/applications"): apps})
    async with client:
        r = await call(client, "get_saved_applications")
        one = await call(client, "get_saved_applications", {"limit": 1})
    assert r["ok"] and r["data"]["applications"] == [
        {"application_id": 1, "title": "Role 1", "company": "Co", "reason": "draft_saved",
         "blanks": ["Notice period?", "Salary?"], "consent_boxes": 1},
        {"application_id": 2, "title": "Role 2", "company": "Co", "reason": "draft_unsaved", "blanks": [], "consent_boxes": 0}]
    assert r["data"]["waiting_for_approval"] == 1 and r["data"]["blocked"] == 1
    assert "2 applications saved" in r["say"] and "2 questions still blank" in r["say"]
    assert len(one["data"]["applications"]) == 1


async def test_saved_applications_none():
    _, client = make({("GET", "/api/applications"): (200, [])})
    async with client:
        r = await call(client, "get_saved_applications")
    assert r["ok"] and r["data"] == {"applications": [], "waiting_for_approval": 0, "blocked": 0}


# ---- get_open_questions ----

def question_routes():
    drafts = [saved(1, "draft_saved", [("Notice period?", "(empty: x)"), ("Expected salary?", "(empty: x)"), ("Name", "K")]),
              saved(2, "draft_unsaved", [("notice  PERIOD", "(empty: x)")]),            # same question, listed once
              saved(3, "", [("Waiting?", "(empty: x)")]),                                 # not a saved draft
              saved(4, "draft_saved", [("Demo?", "(empty: x)")], example=True)]
    answers = [{"id": 1, "question": "Expected salary?", "answer": "12 LPA"},            # the bank answers this one
               {"id": 2, "question": "Visa status?", "answer": ""},
               {"id": 3, "question": "Thank you for your time!", "answer": ""},          # chatbot closing line
               {"id": 4, "question": "Demo bank?", "answer": "", "is_example": True},
               {"id": 5, "question": "Notice period?", "answer": ""}]                     # already asked by a draft
    return {("GET", "/api/applications"): (200, drafts), ("GET", "/api/answers"): (200, answers)}


async def test_open_questions_dedupe_filter_and_sources():
    _, client = make(question_routes())
    async with client:
        r = await call(client, "get_open_questions", {"limit": 5})
    assert r["ok"] and r["data"]["questions"] == [
        {"question": "Notice period?", "job": "Role 1 at Co", "source": "application"},
        {"question": "Visa status?", "job": None, "source": "answer_bank"}]
    assert r["data"]["total"] == 2


async def test_open_questions_one_at_a_time_and_skip():
    _, client = make(question_routes())
    async with client:
        first = await call(client, "get_open_questions")
        skipped = await call(client, "get_open_questions", {"skip": ["notice period"]})
        none_left = await call(client, "get_open_questions", {"skip": ["Notice period?", "visa status"]})
    assert [q["question"] for q in first["data"]["questions"]] == ["Notice period?"] and first["data"]["total"] == 2
    assert first["say"] == "Notice period? 1 more question after that."
    assert [q["question"] for q in skipped["data"]["questions"]] == ["Visa status?"] and skipped["data"]["total"] == 1
    assert none_left["data"] == {"questions": [], "total": 0} and none_left["say"] == "There are no open questions."


async def test_open_question_text_is_cut_at_300_characters():
    long = "Why do you want this job? " * 30
    _, client = make({("GET", "/api/applications"): (200, []),
                      ("GET", "/api/answers"): (200, [{"id": 1, "question": long, "answer": ""}])})
    async with client:
        r = await call(client, "get_open_questions", {"skip": [long]})
        shown = await call(client, "get_open_questions")
    assert len(shown["data"]["questions"][0]["question"]) <= 300 and r["data"]["total"] == 0


# ---- save_answer ----

def answer_routes(existing, saved_id=9):
    return {("GET", "/api/answers"): (200, existing), ("POST", "/api/answers"): (201, {"id": saved_id}),
            ("PUT", f"/api/answers/{existing[0]['id']}" if existing else "/api/answers/0"): (200, {"id": existing[0]["id"] if existing else 0})}


async def test_save_answer_without_confirmation_writes_nothing():
    for args in ({}, {"confirmed": False}):
        fake, client = make(answer_routes([]))
        async with client:
            r = await call(client, "save_answer", {"question": "Notice period?", "answer": "30 days", **args})
        assert not r["ok"] and r["say"].startswith("Needs confirmation:") and r["data"] == {"needs_confirmation": True}
        assert "30 days" in r["say"] and fake.requests == []                  # not even a lookup


async def test_save_answer_creates_with_the_voice_tag():
    fake, client = make(answer_routes([]))
    async with client:
        r = await call(client, "save_answer", {"question": "  Notice   period? ", "answer": " 30  days ", "confirmed": True})
    assert r["ok"] and r["data"] == {"answer_id": 9, "action": "created"}
    assert json.loads(fake.writes[0].content) == {"question": "Notice period?", "answer": "30 days", "tags": "voice"}


async def test_save_answer_updates_a_different_answer_to_the_same_question():
    existing = [{"id": 4, "question": "Notice period?", "answer": "30 days"}]
    fake, client = make(answer_routes(existing))
    async with client:
        r = await call(client, "save_answer", {"question": "notice period", "answer": "60 days", "confirmed": True})
    assert r["data"] == {"answer_id": 4, "action": "updated"}
    assert [(w.method, w.url.path, json.loads(w.content)) for w in fake.writes] == [("PUT", "/api/answers/4", {"answer": "60 days"})]


async def test_save_answer_fills_an_open_question():
    existing = [{"id": 4, "question": "Notice period?", "answer": ""}]
    fake, client = make(answer_routes(existing))
    async with client:
        r = await call(client, "save_answer", {"question": "Notice period?", "answer": "30 days", "confirmed": True})
    assert r["data"]["action"] == "updated" and fake.writes[0].method == "PUT"


async def test_save_answer_unchanged_writes_nothing():
    existing = [{"id": 4, "question": "Notice period?", "answer": "30 days"}]
    fake, client = make(answer_routes(existing))
    async with client:
        r = await call(client, "save_answer", {"question": "Notice period?", "answer": "30   days", "confirmed": True})
    assert r["ok"] and r["data"] == {"answer_id": 4, "action": "unchanged"} and fake.writes == []


async def test_save_answer_ignores_demo_rows():
    existing = [{"id": 4, "question": "Notice period?", "answer": "old", "is_example": True}]
    fake, client = make(answer_routes(existing))
    async with client:
        r = await call(client, "save_answer", {"question": "Notice period?", "answer": "30 days", "confirmed": True})
    assert r["data"]["action"] == "created" and fake.writes[0].method == "POST"


@pytest.mark.parametrize("question,answer", [("", "yes"), ("x", "yes"), ("Notice period?", ""), ("Notice period?", "   "),
                                             ("q" * 301, "yes"), ("Notice period?", "a" * 501)])
async def test_save_answer_short_or_empty_input_fails(question, answer):
    for confirmed in (True, False):
        fake, client = make(answer_routes([]))
        async with client:
            r = await call(client, "save_answer", {"question": question, "answer": answer, "confirmed": confirmed})
        assert not r["ok"] and r["say"] == "I need both the question and the answer to save it." and fake.requests == []


async def test_save_answer_api_error_is_speakable():
    _, client = make({("GET", "/api/answers"): (200, []), ("POST", "/api/answers"): (403, {"detail": "API token not accepted"})})
    async with client:
        r = await call(client, "save_answer", {"question": "Notice period?", "answer": "30 days", "confirmed": True})
    assert not r["ok"] and r["say"] == "API token not accepted"
