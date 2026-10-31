"""JobPilot MCP server: each tool, with the REST API faked at the HTTP layer."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError

from jobpilot.mcp_server.api_client import ApiClient
from jobpilot.mcp_server.server import create_app, create_mcp_server

PACKAGE = Path(__file__).resolve().parents[1] / "jobpilot" / "mcp_server"
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeApi:
    """Records every request and answers from a {(METHOD, path): response} table."""

    def __init__(self, routes: dict | None = None):
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.routes.get((request.method, request.url.path))
        if isinstance(reply, Exception):
            raise reply
        status, body = reply if reply else (404, {"detail": "Not found"})
        return httpx.Response(status, json=body)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def query(self) -> list[tuple[str, str]]:
        return list(self.last.url.params.multi_items())


def make(routes: dict | None = None) -> tuple[FakeApi, Client]:
    fake = FakeApi(routes)
    api = ApiClient(transport=httpx.MockTransport(fake))
    return fake, Client(create_mcp_server(api))


async def call(client: Client, tool: str, args: dict | None = None) -> dict:
    result = await client.call_tool(tool, args or {})
    sc = result.structured_content
    if sc is not None:
        return sc.get("result", sc) if "ok" not in sc else sc
    return json.loads(result.content[0].text)


def job(i, title, company, score=80, status="shortlisted"):
    return {"id": i, "title": title, "company": company, "score": score, "ai_tier": "AI-core", "status": status,
            "url": f"https://x/{i}", "notes": "ignored"}


# ---- listing ----

async def test_exactly_the_seven_tools_with_hints():
    _, client = make()
    async with client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"get_jobs", "get_shortlist", "get_review_queue", "get_run_logs", "run_agent",
                          "approve_application", "skip_application"}
    for name in ("get_jobs", "get_shortlist", "get_review_queue", "get_run_logs"):
        assert tools[name].annotations.read_only_hint is True
    for name in ("run_agent", "approve_application", "skip_application"):
        assert tools[name].annotations.destructive_hint is True


# ---- get_jobs ----

async def test_get_jobs_passes_filters_and_trims_rows():
    fake, client = make({("GET", "/api/jobs"): (200, {"total": 2, "items": [job(1, "ML Engineer", "Acme", status="found"),
                                                                           job(2, "Analyst", "Zeta", status="found")]})})
    async with client:
        r = await call(client, "get_jobs", {"status": "found", "min_score": 70, "query": " python ", "limit": 500})
    q = fake.query()
    assert ("status", "found") in q and ("min_score", "70.0") in q and ("q", "python") in q
    assert ("limit", "25") in q and ("include_examples", "false") in q and ("sort", "score") in q
    assert r["ok"] and "2 found jobs" in r["say"] and "ML Engineer at Acme" in r["say"]
    assert "notes" not in r["data"]["jobs"][0] and r["data"]["total"] == 2


async def test_get_jobs_rejects_unknown_status_without_calling_the_api():
    fake, client = make()
    async with client:
        r = await call(client, "get_jobs", {"status": "banana"})
    assert not r["ok"] and "banana" in r["say"] and fake.requests == []


async def test_get_jobs_empty_and_api_down():
    _, client = make({("GET", "/api/jobs"): (200, {"total": 0, "items": []})})
    async with client:
        assert (await call(client, "get_jobs"))["say"] == "No jobs found."
    _, client = make({("GET", "/api/jobs"): httpx.ConnectError("refused")})
    async with client:
        r = await call(client, "get_jobs")
    assert not r["ok"] and "isn't running" in r["say"]


# ---- get_shortlist ----

async def test_shortlist_asks_for_shortlisted_and_docs_ready_best_first():
    fake, client = make({("GET", "/api/jobs"): (200, {"total": 5, "items": [
        job(i, f"Role {i}", f"Co {i}", status="docs_ready") for i in range(1, 6)]})})
    async with client:
        r = await call(client, "get_shortlist", {})
    q = fake.query()
    assert ("status", "shortlisted") in q and ("status", "docs_ready") in q
    assert ("sort", "score") in q and ("order", "desc") in q
    assert r["say"].startswith("You have 5 on your shortlist.")
    assert "Role 3 at Co 3" in r["say"] and "Role 4" not in r["say"]      # only three are spoken
    assert len(r["data"]["jobs"]) == 5                                     # but all are returned


async def test_shortlist_empty_and_error():
    _, client = make({("GET", "/api/jobs"): (200, {"total": 0, "items": []})})
    async with client:
        assert (await call(client, "get_shortlist"))["say"] == "Your shortlist is empty."
    _, client = make({("GET", "/api/jobs"): (500, {"detail": "boom"})})
    async with client:
        r = await call(client, "get_shortlist")
    assert not r["ok"] and r["say"] == "boom"


# ---- get_review_queue ----

def application(i, title, company, example=False, needs=0, reason=""):
    return {"id": i, "state": "pending_review", "reason": reason, "needs_review": needs, "is_example": example,
            "job": {"title": title, "company": company}}


async def test_review_queue_lists_pending_and_hides_examples():
    fake, client = make({("GET", "/api/applications"): (200, [application(7, "ML Engineer", "Acme", needs=2),
                                                              application(8, "Fake", "Demo", example=True)])})
    async with client:
        r = await call(client, "get_review_queue")
    assert fake.query() == [("state", "pending_review")]
    assert r["say"] == "1 application waiting for approval: application 7, ML Engineer at Acme."
    assert r["data"]["applications"][0]["application_id"] == 7 and r["data"]["applications"][0]["needs_review"] == 2


async def test_review_queue_empty_and_error():
    _, client = make({("GET", "/api/applications"): (200, [])})
    async with client:
        assert (await call(client, "get_review_queue"))["say"] == "Nothing is waiting for approval."
    _, client = make({("GET", "/api/applications"): httpx.ConnectError("x")})
    async with client:
        assert not (await call(client, "get_review_queue"))["ok"]


# ---- get_run_logs ----

async def test_run_logs_recent_runs_for_one_agent():
    runs = [{"id": 3, "agent": "discovery", "status": "error", "error": "Chrome closed", "started_at": "t", "finished_at": "t2"},
            {"id": 2, "agent": "discovery", "status": "ok", "error": ""}]
    fake, client = make({("GET", "/api/runs"): (200, runs)})
    async with client:
        r = await call(client, "get_run_logs", {"agent": "discovery"})
    assert ("agent", "discovery") in fake.query() and ("limit", "5") in fake.query()
    assert r["say"] == "The latest run was discovery, error. Error: Chrome closed"
    assert len(r["data"]["runs"]) == 2


async def test_run_logs_one_run_returns_tail_only():
    log = "\n".join(f"line {i}" for i in range(200))
    fake, client = make({("GET", "/api/runs/9"): (200, {"id": 9, "agent": "apply", "status": "ok", "error": "", "log": log})})
    async with client:
        r = await call(client, "get_run_logs", {"run_id": 9, "tail": 5})
        big = await call(client, "get_run_logs", {"run_id": 9, "tail": 5000})
    assert r["data"]["log"] == [f"line {i}" for i in range(195, 200)]
    assert len(big["data"]["log"]) == 100
    assert r["say"] == "Run 9, apply, is ok."


async def test_run_logs_errors():
    fake, client = make({("GET", "/api/runs/404"): (404, {"detail": "Run not found"})})
    async with client:
        missing = await call(client, "get_run_logs", {"run_id": 404})
        bad = await call(client, "get_run_logs", {"agent": "pipeline"})
    assert not missing["ok"] and missing["say"] == "Run not found"
    assert not bad["ok"] and len(fake.requests) == 1                         # bad agent never reached the API


# ---- run_agent ----

@pytest.mark.parametrize("agent", ["discovery", "scoring", "documents"])
async def test_run_agent_starts_plain_agents_with_no_options(agent):
    fake, client = make({("POST", f"/api/runs/{agent}"): (202, {"id": 12, "agent": agent, "status": "running"})})
    async with client:
        r = await call(client, "run_agent", {"agent": agent})
    assert r["ok"] and r["say"] == f"{agent.capitalize()} started." and r["data"]["run_id"] == 12
    assert fake.last.content in (b"", b"null")


async def test_run_agent_apply_defaults_to_review_only():
    fake, client = make({("POST", "/api/runs/apply"): (202, {"id": 13, "auto_submit": False})})
    async with client:
        r = await call(client, "run_agent", {"agent": "apply"})
    assert json.loads(fake.last.content) == {"auto_submit": False}
    assert "stops at review" in r["say"] and r["data"]["auto_submit"] is False


async def test_run_agent_pipeline():  # JPA-TC-001: no auto_submit argument means a review-only run
    fake, client = make({("POST", "/api/runs/pipeline"): (202, {"started": ["discovery"], "log": "x", "auto_submit": False})})
    async with client:
        r = await call(client, "run_agent", {"agent": "pipeline"})
    assert fake.last.url.path == "/api/runs/pipeline" and json.loads(fake.last.content) == {"auto_submit": False}
    assert r["ok"] and "pipeline started" in r["say"] and "stops at review" in r["say"] and "submitted" not in r["say"]


async def test_run_agent_pipeline_auto_submit_is_passed_through():  # JPA-TC-002
    fake, client = make({("POST", "/api/runs/pipeline"): (202, {"started": ["discovery"], "log": "x", "auto_submit": True})})
    async with client:
        r = await call(client, "run_agent", {"agent": "pipeline", "auto_submit": True})
    assert json.loads(fake.last.content) == {"auto_submit": True}
    assert r["ok"] and "will be submitted" in r["say"] and r["data"]["auto_submit"] is True


async def test_run_agent_says_so_when_the_settings_switch_overrode_the_request():
    fake, client = make({("POST", "/api/runs/pipeline"): (202, {"started": ["discovery"], "log": "x", "auto_submit": False})})
    async with client:
        r = await call(client, "run_agent", {"agent": "pipeline", "auto_submit": True})
    assert json.loads(fake.last.content) == {"auto_submit": True}
    assert "off in Settings" in r["say"] and "will be submitted" not in r["say"]


async def test_run_agent_unknown_agent_never_reaches_the_api():
    fake, client = make()
    async with client:
        try:
            r = await client.call_tool("run_agent", {"agent": "rm -rf"})
            assert r.is_error
        except MCPError:
            pass
    assert fake.requests == []


async def test_run_agent_already_running_is_speakable():
    msg = {"detail": {"message": "A discovery run is already in progress", "run_id": 4}}
    _, client = make({("POST", "/api/runs/discovery"): (409, msg)})
    async with client:
        r = await call(client, "run_agent", {"agent": "discovery"})
    assert not r["ok"] and r["say"] == "A discovery run is already in progress"


# ---- approve / skip ----

@pytest.mark.parametrize("tool,verb,past", [("approve_application", "approve", "Approved"), ("skip_application", "skip", "Skipped")])
async def test_decide_posts_to_the_matching_endpoint(tool, verb, past):
    done = {"id": 7, "state": "approved" if verb == "approve" else "skipped", "job": {"title": "ML Engineer", "company": "Acme"}}
    fake, client = make({("POST", f"/api/applications/7/{verb}"): (200, done)})
    async with client:
        r = await call(client, tool, {"application_id": 7})
    assert fake.last.method == "POST" and fake.last.url.path == f"/api/applications/7/{verb}"
    assert r["ok"] and r["say"] == f"{past} application 7, ML Engineer at Acme." and r["data"]["state"] == done["state"]


@pytest.mark.parametrize("tool,verb", [("approve_application", "approve"), ("skip_application", "skip")])
async def test_decide_passes_conflict_text_through(tool, verb):
    text = "Application 7 is 'submitted', not waiting for review"
    _, client = make({("POST", f"/api/applications/7/{verb}"): (409, {"detail": text})})
    async with client:
        r = await call(client, tool, {"application_id": 7})
    assert not r["ok"] and r["say"] == text


@pytest.mark.parametrize("tool", ["approve_application", "skip_application"])
async def test_decide_rejects_bad_ids_and_missing_applications(tool):
    fake, client = make({("POST", "/api/applications/99/approve"): (404, {"detail": "Application 99 not found"}),
                         ("POST", "/api/applications/99/skip"): (404, {"detail": "Application 99 not found"})})
    async with client:
        zero = await call(client, tool, {"application_id": 0})
        gone = await call(client, tool, {"application_id": 99})
    assert not zero["ok"] and len(fake.requests) == 1
    assert not gone["ok"] and "99" in gone["say"]


# ---- constraints ----

def test_package_has_no_database_access():
    banned = ("jobpilot.db", "sqlmodel", "sqlalchemy", "sqlite3", "alembic")
    for path in PACKAGE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not [n for n in names if n.startswith(banned)], f"{path.name} imports {names}"


def test_importing_the_server_does_not_load_the_database_layer():
    code = "import sys, jobpilot.mcp_server.server; bad=[m for m in sys.modules if m.startswith(('jobpilot.db','sqlmodel','sqlalchemy'))]; print(bad)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=PACKAGE.parents[1], check=True)
    assert out.stdout.strip() == "[]"


async def test_bearer_token_required():
    app = create_app(ApiClient(transport=httpx.MockTransport(FakeApi())), "t" * 20)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as hc:
        assert (await hc.post("/mcp")).status_code == 401
        assert (await hc.post("/mcp", headers={"Authorization": "Bearer wrong"})).status_code == 401
    empty = create_app(ApiClient(transport=httpx.MockTransport(FakeApi())), "")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=empty), base_url="http://127.0.0.1") as hc:
        assert (await hc.post("/mcp", headers={"Authorization": "Bearer "})).status_code == 401   # fails closed


def test_main_refuses_to_start_without_a_token(monkeypatch, capsys):
    from jobpilot.mcp_server import __main__ as entry
    monkeypatch.setattr(entry, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("JOBPILOT_MCP_TOKEN", raising=False)
    assert entry.main() == 2
    assert "JOBPILOT_MCP_TOKEN" in capsys.readouterr().err
