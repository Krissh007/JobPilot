"""Who may call the API: Host allow-list, Origin check, dashboard header, bearer token (jobpilot/api/security.py)."""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from jobpilot.api import security
from jobpilot.api.main import create_app
from jobpilot.api.routers import runs as runs_router
from jobpilot.db.models import AgentName, AgentRun, RunStatus
from jobpilot.db.session import get_engine
from jobpilot.mcp_server.api_client import ApiClient

TOKEN = "k" * 40          # made-up values, only for these tests
OTHER = "z" * 40
DASH = {"X-JobPilot-Client": "dashboard"}
BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[1]


def state_changing(app) -> list[tuple[str, str]]:
    """Every POST/PUT/PATCH/DELETE operation the app publishes (OpenAPI is the public list of what it serves)."""
    return sorted((method.upper(), path) for path, ops in app.openapi()["paths"].items() for method in ops
                  if method.upper() not in security.SAFE_METHODS)


ROUTES = state_changing(create_app(web_dist=None, test_mode=True))


def walk(routes):
    """The app's own route objects, looking inside included routers."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from walk(inner.routes)
        else:
            yield route


def make(db_url=None, token=TOKEN, **kw):
    return create_app(db_url, web_dist=None, api_token=token, **kw)


def client_for(app, base=BASE, **kw):
    return TestClient(app, base_url=base, **kw)


class FakePopen:
    started: list = []

    def __init__(self, command, **kwargs):
        FakePopen.started.append((command, kwargs["env"]))

    def poll(self):
        return None


@pytest.fixture
def fake_popen(monkeypatch):
    FakePopen.started = []
    monkeypatch.setattr(runs_router.subprocess, "Popen", FakePopen)
    return FakePopen


# ---- the route table ----

def test_every_state_changing_route_is_in_the_table():
    """Guard: the table JPS-TC-002 runs over comes from OpenAPI. It must match the route objects themselves, so a
    POST/PUT/PATCH/DELETE route (or a mount, or a websocket) can't be served without being covered by that test."""
    app = create_app(web_dist=None, test_mode=True)
    objects = list(walk(app.routes))
    found = sorted(("/api" + r.path, m) for r in objects if isinstance(r, APIRoute) for m in r.methods
                   if m not in security.SAFE_METHODS)
    assert found == sorted((path, method) for method, path in ROUTES) and len(ROUTES) >= 18
    others = [r for r in objects if not isinstance(r, APIRoute)]
    assert all(set(getattr(r, "methods", None) or ()) <= {"GET", "HEAD"} for r in others), others   # docs pages only
    assert not [r for r in others if type(r).__name__ in ("WebSocketRoute", "Mount")]


# ---- Host (JPS-TC-001) ----

@pytest.mark.parametrize("host", ["evil.com", "127.0.0.1.nip.io", "127.0.0.1", "localhost", "127.0.0.1:9999",
                                  "[::1]:8000", "127.0.0.1:8000.evil.com", "testserver", ""])
def test_bad_host_is_refused_on_get_and_post(db_url, host):                              # JPS-TC-001
    c = client_for(make(db_url))
    assert c.get("/api/meta", headers={"Host": host}).status_code == 403
    assert c.post("/api/review/weekly/done", headers={**DASH, "Host": host}).status_code == 403
    assert c.options("/api/meta", headers={"Host": host}).status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "LOCALHOST:8000"])
def test_own_hosts_are_accepted(db_url, host):
    assert client_for(make(db_url)).get("/api/meta", headers={"Host": host}).status_code == 200


def test_the_port_is_the_one_the_server_is_bound_to(db_url):
    c = client_for(make(db_url), base="http://127.0.0.1:8123")
    assert c.get("/api/meta").status_code == 200
    assert c.get("/api/meta", headers={"Host": "127.0.0.1:8000"}).status_code == 403


def test_testserver_only_in_test_mode(db_url):
    assert TestClient(make(db_url)).get("/api/meta").status_code == 403
    assert TestClient(make(db_url, test_mode=True)).get("/api/meta").status_code == 200


# ---- dashboard header (JPS-TC-002) ----

def concrete(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: "discovery" if m.group(1) == "agent" else "1", path)


@pytest.mark.parametrize("method,path", ROUTES)
def test_a_change_without_header_or_bearer_is_refused(db_url, method, path):             # JPS-TC-002
    response = client_for(make(db_url)).request(method, concrete(path))
    assert response.status_code == 403, (method, path, response.text)


def test_refused_requests_do_nothing(db_url, fake_popen):
    c = client_for(make(db_url))
    assert c.put("/api/settings", json={"auto_submit": True}).status_code == 403
    assert c.post("/api/runs/pipeline", json={"auto_submit": True}).status_code == 403
    assert c.post("/api/runs/apply").status_code == 403
    assert fake_popen.started == []
    assert client_for(make(db_url), headers=DASH).get("/api/settings").json()["auto_submit"] is False


def test_the_dashboard_header_with_matching_host_and_origin_is_allowed(db_url, fake_popen):
    c = client_for(make(db_url), headers=DASH)
    assert c.post("/api/runs/pipeline", headers={"Origin": BASE}).status_code == 202
    assert c.post("/api/review/weekly/done", headers={"Origin": "http://localhost:8000"}).status_code == 200


def test_only_the_exact_header_value_counts(db_url):
    c = client_for(make(db_url))
    for value in ("", "Dashboard", "agent", "true"):
        assert c.post("/api/review/weekly/done", headers={"X-JobPilot-Client": value}).status_code == 403


# ---- Origin (JPS-TC-003) ----

@pytest.mark.parametrize("origin", ["http://evil.com", "https://127.0.0.1:8000", "http://127.0.0.1:8001", "null",
                                    "http://127.0.0.1.nip.io:8000", "http://127.0.0.1:5173", "http://localhost:5173"])
def test_a_foreign_origin_with_the_dashboard_header_is_refused(db_url, origin):         # JPS-TC-003
    c = client_for(make(db_url), headers=DASH)
    assert c.put("/api/settings", json={"auto_submit": True}, headers={"Origin": origin}).status_code == 403
    assert c.post("/api/runs/pipeline", headers={"Origin": origin}).status_code == 403
    assert c.get("/api/settings").json()["auto_submit"] is False


def test_a_foreign_origin_is_refused_even_with_a_valid_bearer(db_url):
    c = client_for(make(db_url))
    response = c.post("/api/review/weekly/done", headers={"Authorization": f"Bearer {TOKEN}", "Origin": "http://evil.com"})
    assert response.status_code == 403


def test_the_vite_origin_needs_dev_mode(db_url):
    dev = client_for(make(db_url, dev_mode=True), headers=DASH)
    assert dev.post("/api/review/weekly/done", headers={"Origin": "http://localhost:5173"}).status_code == 200
    assert dev.post("/api/review/weekly/done", headers={"Origin": "http://evil.com"}).status_code == 403
    off = client_for(make(db_url, dev_mode=False), headers=DASH)
    assert off.post("/api/review/weekly/done", headers={"Origin": "http://localhost:5173"}).status_code == 403


def test_cors_allows_no_foreign_origin(db_url):
    c = client_for(make(db_url, dev_mode=False))
    preflight = c.options("/api/settings", headers={"Origin": "http://evil.com", "Access-Control-Request-Method": "PUT",
                                                    "Access-Control-Request-Headers": "x-jobpilot-client"})
    assert "access-control-allow-origin" not in preflight.headers
    plain = c.get("/api/meta", headers={"Origin": "http://localhost:5173"})
    assert "access-control-allow-origin" not in plain.headers
    dev = client_for(make(db_url, dev_mode=True)).get("/api/meta", headers={"Origin": "http://localhost:5173"})
    assert dev.headers["access-control-allow-origin"] == "http://localhost:5173"


# ---- bearer token (JPS-TC-004) ----

def test_a_valid_bearer_with_no_origin_is_allowed(db_url):                              # JPS-TC-004
    c = client_for(make(db_url))
    response = c.post("/api/answers", json={"question": "Notice period", "answer": "30", "tags": "t"},
                      headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 201


def test_a_valid_bearer_with_the_apps_own_origin_is_allowed(db_url):
    response = client_for(make(db_url)).post("/api/review/weekly/done", headers={"Authorization": f"Bearer {TOKEN}", "Origin": BASE})
    assert response.status_code == 200


@pytest.mark.parametrize("auth", [f"Bearer {OTHER}", f"Bearer {TOKEN}x", "Bearer", "Bearer ", "Basic abc", TOKEN,
                                  f"Bearer {TOKEN} extra", "Bearer a b"])
def test_a_wrong_or_malformed_bearer_gets_401_and_never_falls_back_to_the_header(db_url, auth):
    c = client_for(make(db_url))
    assert c.post("/api/review/weekly/done", headers={"Authorization": auth}).status_code == 401
    assert c.post("/api/review/weekly/done", headers={"Authorization": auth, **DASH}).status_code == 401


@pytest.mark.parametrize("token", [None, "", "short", "k" * 31])
def test_a_bearer_is_rejected_when_token_auth_is_disabled(db_url, token):
    c = client_for(make(db_url, token=token))
    attempt = token or TOKEN
    assert c.post("/api/review/weekly/done", headers={"Authorization": f"Bearer {attempt}"}).status_code == 401
    assert c.post("/api/review/weekly/done", headers=DASH).status_code == 200            # the dashboard keeps working
    assert c.post("/api/review/weekly/done").status_code == 403


def test_safe_requests_ignore_the_bearer(db_url):
    c = client_for(make(db_url))
    assert c.get("/api/meta", headers={"Authorization": f"Bearer {OTHER}"}).status_code == 200


def test_a_missing_or_short_token_logs_one_warning_without_the_value(db_url, caplog):
    with caplog.at_level(logging.DEBUG):
        make(db_url, token="shorty-but-not-empty")
    warnings = [r for r in caplog.records if r.name == "jobpilot.api.security" and r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "shorty" not in warnings[0].getMessage() and "too short" in warnings[0].getMessage()
    caplog.clear()
    make(db_url)                       # a usable token: no warning
    assert not [r for r in caplog.records if r.name == "jobpilot.api.security"]


def test_the_token_is_read_from_the_environment_then_from_the_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "x.env"
    env_file.write_text(f"{security.TOKEN_ENV}={TOKEN}\nOTHER=1\n", encoding="utf-8")
    monkeypatch.delenv(security.TOKEN_ENV, raising=False)
    assert security.load_api_token(env_file) == TOKEN and security.load_api_token(tmp_path / "missing.env") == ""
    monkeypatch.setenv(security.TOKEN_ENV, OTHER)
    assert security.load_api_token(env_file) == OTHER


# ---- who started a run ----

def runs_in(db_url):
    with Session(get_engine(db_url)) as s:
        return s.exec(select(AgentRun).order_by(AgentRun.id)).all()


def test_runs_record_the_caller_and_what_was_asked(db_url, fake_popen):
    agent = client_for(make(db_url), headers={"Authorization": f"Bearer {TOKEN}"})
    dash = client_for(make(db_url), headers=DASH)
    first = agent.post("/api/runs/apply", json={"auto_submit": True}).json()
    row = runs_in(db_url)[0]
    assert (row.caller, row.auto_submit_requested, row.auto_submit_setting) == ("agent", True, False)
    assert first["auto_submit"] is False and "started by agent" in row.log
    with Session(get_engine(db_url)) as s:
        s.get(AgentRun, row.id).status = RunStatus.ok
        s.commit()
    dash.put("/api/settings", json={"auto_submit": True})
    dash.post("/api/runs/apply")
    second = runs_in(db_url)[1]
    assert (second.caller, second.auto_submit_requested, second.auto_submit_setting) == ("dashboard", False, True)
    assert agent.post("/api/runs/discovery").status_code == 202
    assert runs_in(db_url)[2].auto_submit_requested is None        # not an apply run


def test_the_pipeline_passes_its_origin_to_the_agent_rows(db_url, fake_popen):
    client_for(make(db_url), headers={"Authorization": f"Bearer {TOKEN}"}).post("/api/runs/pipeline", json={"auto_submit": True})
    command, env = fake_popen.started[0]
    assert command[-1] == "--review-only"
    assert (env["JOBPILOT_RUN_CALLER"], env["JOBPILOT_RUN_AUTO_SUBMIT_REQUESTED"], env["JOBPILOT_RUN_AUTO_SUBMIT_SETTING"]) == ("agent", "1", "0")


def test_an_agent_process_records_its_origin_from_the_environment(db_url, monkeypatch):
    from jobpilot.agents._run import agent_run
    monkeypatch.setenv("JOBPILOT_RUN_CALLER", "agent")
    monkeypatch.setenv("JOBPILOT_RUN_AUTO_SUBMIT_REQUESTED", "1")
    monkeypatch.setenv("JOBPILOT_RUN_AUTO_SUBMIT_SETTING", "0")
    with agent_run(AgentName.apply, db_url):
        pass
    with agent_run(AgentName.scoring, db_url):
        pass
    apply_row, scoring_row = runs_in(db_url)
    assert (apply_row.caller, apply_row.auto_submit_requested, apply_row.auto_submit_setting) == ("agent", True, False)
    assert (scoring_row.caller, scoring_row.auto_submit_requested) == ("agent", None)


def test_runs_started_outside_the_api_have_no_caller(db_url, monkeypatch):
    from jobpilot.agents._run import agent_run
    for name in ("JOBPILOT_RUN_CALLER", "JOBPILOT_RUN_AUTO_SUBMIT_REQUESTED", "JOBPILOT_RUN_AUTO_SUBMIT_SETTING"):
        monkeypatch.delenv(name, raising=False)
    with agent_run(AgentName.apply, db_url):
        pass
    row = runs_in(db_url)[0]
    assert (row.caller, row.auto_submit_requested, row.auto_submit_setting) == (None, None, None)


def test_the_new_columns_are_additive_and_old_rows_still_load(tmp_path):
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "0005")
    con = sqlite3.connect(tmp_path / "old.db")
    con.execute("INSERT INTO agent_runs (agent, started_at, found, processed, status, error, log, is_example) "
                "VALUES ('apply', '2026-10-01 00:00:00', 0, 0, 'ok', '', 'old', 0)")
    con.commit()
    con.close()
    command.upgrade(cfg, "head")
    with Session(get_engine(url)) as s:
        row = s.exec(select(AgentRun)).one()
    assert row.log == "old" and (row.caller, row.auto_submit_requested, row.auto_submit_setting) == (None, None, None)


# ---- the token never leaks ----

def test_the_token_never_appears_in_logs_responses_run_records_or_child_environments(db_url, fake_popen, monkeypatch, caplog):
    monkeypatch.setenv(security.TOKEN_ENV, TOKEN)
    app = make(db_url)
    c = client_for(app, headers={"Authorization": f"Bearer {TOKEN}"})
    wrong = client_for(app, headers={"Authorization": f"Bearer {OTHER}"})
    with caplog.at_level(logging.DEBUG):
        responses = [c.post("/api/runs/apply"), c.post("/api/runs/pipeline", json={"auto_submit": True}),
                     c.get("/api/runs"), c.get("/api/system"), c.get("/api/export/all.json"),
                     wrong.post("/api/runs/apply"), wrong.put("/api/settings", json={})]
    seen = " ".join(r.text for r in responses) + " " + caplog.text + " ".join(str(dict(r.headers)) for r in responses)
    seen += " ".join(f"{row.log} {row.error} {row.caller}" for row in runs_in(db_url))
    for value in (TOKEN, OTHER):
        assert value not in seen
    assert [r.status_code for r in responses[5:]] == [401, 401]
    for _, env in fake_popen.started:
        assert security.TOKEN_ENV not in env and TOKEN not in env.values()


# ---- the MCP server's side (JPS-TC-006) ----

@pytest.mark.anyio
async def test_the_mcp_client_sends_the_bearer_on_every_request():                      # JPS-TC-006
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.headers.get("authorization")))
        return httpx.Response(200, json={})

    api = ApiClient(transport=httpx.MockTransport(handler), api_token=TOKEN)
    await api.get("/meta")
    await api.post("/runs/discovery")
    assert seen == [("GET", f"Bearer {TOKEN}"), ("POST", f"Bearer {TOKEN}")]
    bare = ApiClient(transport=httpx.MockTransport(handler))
    await bare.post("/runs/discovery")
    assert seen[-1] == ("POST", None)


@pytest.mark.anyio
async def test_an_api_error_never_repeats_the_token():
    api = ApiClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"detail": "API token not accepted"})), api_token=TOKEN)
    with pytest.raises(Exception) as caught:
        await api.post("/runs/pipeline")
    assert TOKEN not in str(caught.value) and "not accepted" in str(caught.value)


@pytest.mark.parametrize("api_token", [None, "", "k" * 31])
def test_the_mcp_server_refuses_to_start_without_a_usable_api_token(monkeypatch, capsys, api_token):   # JPS-TC-006
    from jobpilot.mcp_server import __main__ as entry
    monkeypatch.setattr(entry, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("JOBPILOT_MCP_TOKEN", "m" * 20)
    monkeypatch.delenv("JOBPILOT_API_TOKEN", raising=False)
    if api_token is not None:
        monkeypatch.setenv("JOBPILOT_API_TOKEN", api_token)
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **k: pytest.fail("the server must not start"))
    assert entry.main() == 2
    err = capsys.readouterr().err
    assert "JOBPILOT_API_TOKEN" in err and "k" * 31 not in err


def test_the_mcp_server_starts_with_both_tokens_and_passes_the_bearer_on(monkeypatch):
    from jobpilot.mcp_server import __main__ as entry
    seen = {}
    monkeypatch.setattr(entry, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("JOBPILOT_MCP_TOKEN", "m" * 20)
    monkeypatch.setenv("JOBPILOT_API_TOKEN", TOKEN)
    monkeypatch.setattr(entry, "create_app", lambda api, token: seen.update(api=api) or "app")
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **k: None)
    assert entry.main() == 0
    assert seen["api"]._http.headers["authorization"] == f"Bearer {TOKEN}"
