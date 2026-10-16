"""Smoke test: every MCP tool against JobPilot's real API app on a real (temp, migrated) database."""
from __future__ import annotations

import json

import httpx
import pytest
from mcp.client.client import Client
from sqlmodel import Session

from jobpilot.api.main import create_app as create_api
from jobpilot.api.routers import runs as runs_router
from jobpilot.db.models import AgentName, AgentRun, Application, ApplicationState, Job, JobStatus, RunStatus
from jobpilot.db.session import get_engine
from jobpilot.mcp_server.api_client import ApiClient
from jobpilot.mcp_server.server import create_mcp_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakePopen:
    started: list[list[str]] = []

    def __init__(self, command, **kwargs):
        FakePopen.started.append(command)

    def poll(self):
        return None


def add_job(session, i, title, status, score):
    job = Job(title=title, company=f"Co{i}", url=f"https://real.example/{i}", url_canonical=f"https://real.example/{i}",
              dedupe_hash=f"h{i}", status=status, score=score)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


async def call(client, tool, args=None):
    result = await client.call_tool(tool, args or {})
    sc = result.structured_content
    return sc if sc is not None and "ok" in sc else (sc or {}).get("result") or json.loads(result.content[0].text)


async def test_every_tool_against_the_real_api(db_url, monkeypatch):
    monkeypatch.setattr(runs_router.subprocess, "Popen", FakePopen)
    FakePopen.started = []
    with Session(get_engine(db_url)) as s:
        add_job(s, 1, "Found Only", JobStatus.found, 40)
        a = add_job(s, 2, "ML Engineer", JobStatus.shortlisted, 90)
        b = add_job(s, 3, "Data Analyst", JobStatus.docs_ready, 80)
        c = add_job(s, 4, "BA Lead", JobStatus.docs_ready, 70)
        apps = []
        for job in (a, b, c):
            app = Application(job_id=job.id, state=ApplicationState.pending_review)
            s.add(app)
            apps.append(app)
        s.add(AgentRun(agent=AgentName.apply, status=RunStatus.running, log="a\nb\nfilling form"))   # apply is holding forms open
        s.commit()
        ids = [x.id for x in apps]

    transport = httpx.ASGITransport(app=create_api(db_url, web_dist=None))
    client = Client(create_mcp_server(ApiClient(transport=transport)))
    async with client:
        jobs = await call(client, "get_jobs", {"status": "found"})
        assert jobs["ok"] and [j["title"] for j in jobs["data"]["jobs"]] == ["Found Only"]

        shortlist = await call(client, "get_shortlist")
        assert shortlist["data"]["total"] == 3
        assert [j["title"] for j in shortlist["data"]["jobs"]] == ["ML Engineer", "Data Analyst", "BA Lead"]

        queue = await call(client, "get_review_queue")
        assert queue["data"]["count"] == 3 and "ML Engineer at Co2" in queue["say"]

        approved = await call(client, "approve_application", {"application_id": ids[0]})
        assert approved["ok"] and approved["data"]["state"] == "approved"
        skipped = await call(client, "skip_application", {"application_id": ids[1]})
        assert skipped["ok"] and skipped["data"]["state"] == "skipped"
        again = await call(client, "skip_application", {"application_id": ids[1]})
        assert not again["ok"] and "not waiting for review" in again["say"]
        assert (await call(client, "get_review_queue"))["data"]["count"] == 1

        started = await call(client, "run_agent", {"agent": "discovery"})
        assert started["ok"] and started["data"]["run_id"]
        dupe = await call(client, "run_agent", {"agent": "discovery"})
        assert not dupe["ok"] and "already in progress" in dupe["say"]
        blocked = await call(client, "run_agent", {"agent": "apply"})           # the seeded apply run is live
        assert not blocked["ok"] and "already in progress" in blocked["say"]
        assert FakePopen.started and "jobpilot.agents.discover" in FakePopen.started[0]

        logs = await call(client, "get_run_logs", {"agent": "discovery"})
        assert logs["ok"] and logs["data"]["runs"][0]["agent"] == "discovery"
        one = await call(client, "get_run_logs", {"run_id": started["data"]["run_id"], "tail": 3})
        assert one["ok"] and one["data"]["run"]["id"] == started["data"]["run_id"]
