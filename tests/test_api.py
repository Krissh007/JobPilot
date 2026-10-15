from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from jobpilot.api.main import create_app
from jobpilot.api.routers import jobs as jobs_router
from jobpilot.api.routers import runs as runs_router
from jobpilot.db.models import (
    Actor,
    AgentName,
    AgentRun,
    Application,
    ApplicationState,
    Job,
    JobStatus,
    RunStatus,
    StatusHistory,
    utcnow,
)
from jobpilot.db.seed import seed
from jobpilot.db.session import get_engine

from tests.test_search_import import make_legacy


@pytest.fixture
def app(db_url):
    application = create_app(db_url)
    application.state.sse_poll_seconds = 0.05
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def seeded(db_url):
    with Session(get_engine(db_url)) as s:
        seed(s)
        s.commit()


def session_for(db_url):
    return Session(get_engine(db_url))


# ---- meta ----

def test_meta_counts(client, seeded):
    meta = client.get("/api/meta").json()
    assert meta["db"] == "ok" and meta["total_jobs"] == 15 and meta["example_jobs"] == 15
    assert meta["stages"] == {"found": 4, "shortlisted": 3, "docs_ready": 2, "applied": 3, "interview": 1, "offer": 1, "rejected": 1}
    assert meta["threshold"] == 60 and meta["daily_apply_cap"] == 10
    assert meta["pending_review"] == 1 and meta["blocked"] == 1
    assert meta["applied_today"] == 1  # Tellapur Digital applied "0 days ago"


def test_non_local_clients_are_refused(app):
    remote = TestClient(app, client=("192.168.1.20", 50000))
    assert remote.get("/api/meta").status_code == 403


# ---- jobs ----

def test_list_filter_sort_and_search(client, seeded):
    body = client.get("/api/jobs").json()
    assert body["total"] == 15 and body["items"][0]["score"] == 86  # score desc by default
    assert body["items"][-1]["score"] is None  # unscored last
    assert "jd_text" not in body["items"][0]

    shortlisted = client.get("/api/jobs", params={"status": ["shortlisted", "docs_ready"]}).json()
    assert shortlisted["total"] == 5
    naukri = client.get("/api/jobs", params={"source": "naukri", "min_score": 70}).json()
    assert {j["company"] for j in naukri["items"]} == {"Deccan Health AI", "Charminar Labs"}
    by_company = client.get("/api/jobs", params={"sort": "company", "order": "asc"}).json()["items"]
    assert by_company[0]["company"] == "Banjara Retail Tech"

    hits = client.get("/api/jobs", params={"q": "forecast"}).json()  # prefix match: forecasting
    assert [j["company"] for j in hits["items"]] == ["Banjara Retail Tech"]
    assert client.get("/api/jobs", params={"q": '"); DROP TABLE jobs; --'}).status_code == 200  # sanitised
    assert client.get("/api/jobs", params={"sort": "jd_text"}).status_code == 422
    assert client.get("/api/jobs", params={"include_examples": False}).json()["total"] == 0


def test_create_job_and_duplicate(client):
    payload = {"url": "https://www.linkedin.com/jobs/view/4099999999/?trk=x", "title": "AI BA", "company": "Acme",
               "location": "Hyderabad", "jd_text": "Own LLM requirements"}
    created = client.post("/api/jobs", json=payload)
    assert created.status_code == 201 and created.json()["status"] == "found"
    again = client.post("/api/jobs", json={**payload, "url": "https://in.linkedin.com/jobs/view/4099999999"})
    assert again.status_code == 409 and again.json()["detail"]["job_id"] == created.json()["id"]
    history = client.get(f"/api/jobs/{created.json()['id']}/history").json()
    assert history[0]["actor"] == "user" and history[0]["to_status"] == "found"


def test_detail_patch_status_notes_and_delete(client, seeded, db_url):
    job_id = client.get("/api/jobs", params={"status": "found", "sort": "score"}).json()["items"][0]["id"]
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["jd_text"] and detail["docs"] == {"resume": False, "cover": False, "report": False, "folder": None}

    moved = client.patch(f"/api/jobs/{job_id}", json={"status": "shortlisted", "notes": "ping recruiter"}).json()
    assert moved["status"] == "shortlisted" and moved["notes"] == "ping recruiter"
    last = client.get(f"/api/jobs/{job_id}/history").json()[-1]
    assert (last["from_status"], last["to_status"], last["actor"]) == ("found", "shortlisted", "user")
    assert client.patch(f"/api/jobs/{job_id}", json={"status": "ghosted"}).status_code == 422

    assert client.delete(f"/api/jobs/{job_id}").status_code == 204
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    with session_for(db_url) as s:
        assert not s.exec(select(StatusHistory).where(StatusHistory.job_id == job_id)).all()


def test_docs_download_and_open_folder(client, db_url, tmp_path, monkeypatch):
    folder = tmp_path / "output" / "Acme_1"
    folder.mkdir(parents=True)
    (folder / "Resume.docx").write_bytes(b"PK resume")
    (folder / "match_report.md").write_text("# report", encoding="utf-8")
    job_id = client.post("/api/jobs", json={"url": "https://example.com/j/1", "title": "BA", "company": "Acme"}).json()["id"]
    with session_for(db_url) as s:
        job = s.get(Job, job_id)
        job.resume_path = str(folder / "Resume.docx")
        s.add(job)
        s.commit()

    assert client.get(f"/api/jobs/{job_id}/docs/resume").content == b"PK resume"
    assert client.get(f"/api/jobs/{job_id}/docs/report").text == "# report"
    assert client.get(f"/api/jobs/{job_id}/docs/cover").status_code == 404
    opened = []
    monkeypatch.setattr(jobs_router.os, "startfile", opened.append, raising=False)
    assert client.post(f"/api/jobs/{job_id}/open-folder").json() == {"opened": str(folder)}
    assert opened == [folder]


# ---- review queue ----

def test_review_queue_approve_and_skip_rules(client, seeded):
    pending = client.get("/api/applications", params={"state": "pending_review"}).json()
    assert len(pending) == 1
    app = pending[0]
    assert app["job"]["company"] == "Banjara Retail Tech" and app["needs_review"] == 2 and len(app["fields"]) == 4
    assert client.get(f"/api/applications/{app['id']}/screenshot").status_code == 404

    approved = client.post(f"/api/applications/{app['id']}/approve").json()
    assert approved["state"] == "approved" and approved["decided_at"]
    assert client.post(f"/api/applications/{app['id']}/skip").status_code == 409  # already decided
    blocked = client.get("/api/applications", params={"state": "blocked"}).json()[0]
    assert client.post(f"/api/applications/{blocked['id']}/approve").status_code == 409


# ---- answers & settings ----

def test_answers_crud(client):
    created = client.post("/api/answers", json={"question": "Notice period?", "answer": "30 days", "tags": "notice"}).json()
    updated = client.put(f"/api/answers/{created['id']}", json={"answer": "Immediate"}).json()
    assert updated["answer"] == "Immediate" and updated["question"] == "Notice period?"
    assert client.post("/api/answers", json={"question": "", "answer": "x"}).status_code == 422
    assert [a["id"] for a in client.get("/api/answers").json()] == [created["id"]]
    assert client.delete(f"/api/answers/{created['id']}").status_code == 204
    assert client.get("/api/answers").json() == []


def test_settings_validation(client):
    assert client.get("/api/settings").json()["threshold"] == 60
    changed = client.put("/api/settings", json={"threshold": 70, "search_queries": [" AI BA ", "AI BA", ""]}).json()
    assert changed["threshold"] == 70 and changed["search_queries"] == ["AI BA"] and changed["daily_apply_cap"] == 10
    assert client.put("/api/settings", json={"threshold": 10}).status_code == 422
    assert client.put("/api/settings", json={"daily_apply_cap": 100}).status_code == 422
    assert client.put("/api/settings", json={"search_queries": [" "]}).status_code == 422
    assert client.get("/api/meta").json()["threshold"] == 70


# ---- analytics & export ----

def test_analytics(client, seeded):
    funnel = {s["stage"]: s["count"] for s in client.get("/api/analytics/funnel").json()["stages"]}
    assert funnel == {"found": 15, "shortlisted": 10, "docs_ready": 7, "applied": 5, "interview": 2, "offer": 1}
    hist = client.get("/api/analytics/score-histogram").json()
    assert sum(b["count"] for b in hist["bins"]) == 14 and hist["unscored"] == 1 and hist["bins"][8]["count"] == 2
    per_day = client.get("/api/analytics/applications-per-day", params={"days": 30}).json()["days"]
    assert len(per_day) == 30 and per_day[-1]["count"] == 1 and sum(d["count"] for d in per_day) == 5


def test_exports(client, seeded):
    csv_response = client.get("/api/export/jobs.csv")
    assert csv_response.headers["content-type"].startswith("text/csv")
    lines = csv_response.content.decode("utf-8-sig").strip().splitlines()
    assert lines[0].startswith("id,title,company") and len(lines) == 16
    dump = client.get("/api/export/all.json").json()
    assert len(dump["jobs"]) == 15 and len(dump["application_fields"]) == 4 and dump["settings"]


def test_examples_purge_and_seed(client, seeded):
    assert client.delete("/api/examples").json()["jobs"] == 15
    assert client.get("/api/meta").json()["total_jobs"] == 0
    assert client.post("/api/examples/seed").json() == {"added_jobs": 15}


# ---- agent runs ----

class FakePopen:
    started: list[list[str]] = []

    def __init__(self, command, **kwargs):
        FakePopen.started.append(command)
        self.env = kwargs["env"]

    def poll(self):
        return None


def test_start_run_builds_whitelisted_command(client, monkeypatch):
    FakePopen.started = []
    monkeypatch.setattr(runs_router.subprocess, "Popen", FakePopen)
    response = client.post("/api/runs/score", json={"limit": 5})
    assert response.status_code == 202 and response.json()["status"] == "running"
    run_id = response.json()["id"]
    command = FakePopen.started[0]
    assert command[1:] == ["-m", "jobpilot.agents.score", "--run-id", str(run_id), "--limit", "5"]

    busy = client.post("/api/runs/score")
    assert busy.status_code == 409 and busy.json()["detail"]["run_id"] == run_id
    assert client.post("/api/runs/search", json={"import_legacy_db": True, "then_score": True}).status_code == 202
    assert FakePopen.started[1][-2:] == ["--import-legacy-db", "--then-score"]
    assert client.post("/api/runs/apply", json={"job_id": 7, "review_only": True}).status_code == 202
    assert FakePopen.started[2][1:] == ["-m", "jobpilot.agents.apply", "--run-id", FakePopen.started[2][4], "--job-id", "7", "--review-only"]
    assert client.post("/api/runs/score", json={"limit": 9999}).status_code == 422


def test_stale_running_row_does_not_block(client, db_url, monkeypatch):
    monkeypatch.setattr(runs_router.subprocess, "Popen", FakePopen)
    with session_for(db_url) as s:
        s.add(AgentRun(agent=AgentName.score, status=RunStatus.running, started_at=utcnow() - timedelta(hours=5)))
        s.commit()
    assert client.post("/api/runs/score").status_code == 202


def sse_events(text: str) -> list[tuple[str, str]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if not line.startswith(":"))
        if lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_stream_replays_log_and_ends(client, db_url):
    with session_for(db_url) as s:
        run = AgentRun(agent=AgentName.score, status=RunStatus.ok, log="line one\nline two", finished_at=utcnow())
        s.add(run)
        s.commit()
        run_id = run.id
    events = sse_events(client.get(f"/api/runs/{run_id}/stream").text)
    assert events == [("log", "line one"), ("log", "line two"), ("end", {"status": "ok", "error": ""})]
    assert client.get("/api/runs/999/stream").status_code == 404


def test_real_subprocess_run_streams_to_completion(client, db_url, tmp_path, monkeypatch):
    """End to end: POST starts a real agent process; the SSE stream follows it until it finishes."""
    legacy = make_legacy(tmp_path)
    monkeypatch.setenv("LEGACY_AGENT_DIR", str(legacy))
    run_id = client.post("/api/runs/search", json={"import_legacy_db": True}).json()["id"]

    events = sse_events(client.get(f"/api/runs/{run_id}/stream").text)
    assert events[-1] == ("end", {"status": "ok", "error": ""})
    log_lines = [data for kind, data in events if kind == "log"]
    assert log_lines[0] == "Starting..." and log_lines[1].endswith(f"search run #{run_id} started")
    assert any("3 new, 0 updated, 1 skipped" in line for line in log_lines)
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "ok" and (run["found"], run["processed"]) == (3, 4)
    assert client.get("/api/meta").json()["total_jobs"] == 3


def test_crashed_process_is_marked_error(client, db_url, monkeypatch):
    class DeadPopen(FakePopen):
        def poll(self):
            return 1

    monkeypatch.setattr(runs_router.subprocess, "Popen", DeadPopen)
    run_id = client.post("/api/runs/score").json()["id"]
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "error" and "exited with code 1" in run["error"]


def test_table_counts_and_system_info_never_leak_key(client, seeded):
    counts = client.get("/api/stats/tables").json()
    assert counts["jobs"] == 15 and counts["application_fields"] == 4 and counts["settings"] == 9
    info = client.get("/api/system").json()
    assert set(info) >= {"gemini_model", "gemini_key_set", "legacy_agent_dir", "resume_path"}
    from jobpilot.config import get_config
    key = get_config().gemini_api_key
    assert not key or key not in json.dumps(info)


def test_approve_requires_a_waiting_apply_agent(client, db_url):
    with session_for(db_url) as s:
        job = Job(title="BA", company="Acme", url="https://example.com/a", url_canonical="a", dedupe_hash="h")
        s.add(job)
        s.commit()
        app = Application(job_id=job.id, state=ApplicationState.pending_review)
        s.add(app)
        s.commit()
        app_id = app.id
    refused = client.post(f"/api/applications/{app_id}/approve")
    assert refused.status_code == 409 and "no longer holding" in refused.json()["detail"]
    with session_for(db_url) as s:
        s.add(AgentRun(agent=AgentName.apply, status=RunStatus.running))
        s.commit()
    assert client.post(f"/api/applications/{app_id}/approve").json()["state"] == "approved"


def test_weekly_review_cycle(client, db_url):
    from jobpilot.db.models import Answer

    with session_for(db_url) as s:
        s.add(Answer(question="Expected CTC (LPA)", answer="", tags="needs-answer"))
        s.add(Answer(question="Notice period", answer="30 days", tags=""))
        s.commit()
    info = client.get("/api/review/weekly").json()
    assert info["due"] and [q["question"] for q in info["open_questions"]] == ["Expected CTC (LPA)"]
    oq = info["open_questions"][0]["id"]
    assert "needs-answer" not in client.put(f"/api/answers/{oq}", json={"answer": "12"}).json()["tags"]
    done = client.post("/api/review/weekly/done").json()
    assert not done["due"] and done["open_questions"] == [] and done["reviewed_at"]


def test_last_due_is_monday_0900_ist():
    from datetime import datetime, timezone

    from jobpilot.weekly import last_due

    # Wed 7 Oct 2026 11:00 IST -> due Mon 5 Oct 09:00 IST
    assert last_due(datetime(2026, 10, 7, 5, 30, tzinfo=timezone.utc), "mon", "09:00").isoformat() == "2026-10-05T09:00:00+05:30"
    # Mon 5 Oct 08:00 IST, before 09:00 -> the previous Monday
    assert last_due(datetime(2026, 10, 5, 2, 30, tzinfo=timezone.utc), "mon", "09:00").isoformat() == "2026-09-28T09:00:00+05:30"


def test_job_list_carries_the_ai_tier(client, db_url):
    from sqlmodel import Session

    from jobpilot.db.models import Job
    from jobpilot.db.repo import upsert_job
    from jobpilot.db.session import get_engine

    with Session(get_engine(db_url)) as s:
        job, _ = upsert_job(s, {"title": "AI BA", "company": "TierCo", "url": "https://www.linkedin.com/jobs/view/4999999991",
                                "score_reason": "Great fit. [Tier: AI-adjacent]"})
        plain, _ = upsert_job(s, {"title": "Old", "company": "NoTier", "url": "https://www.linkedin.com/jobs/view/4999999992",
                                  "score_reason": "Scored by the old agent."})
        s.commit()
    items = {i["company"]: i for i in client.get("/api/jobs", params={"include_examples": "true", "limit": 500}).json()["items"]}
    assert items["TierCo"]["ai_tier"] == "AI-adjacent" and items["NoTier"]["ai_tier"] == ""
