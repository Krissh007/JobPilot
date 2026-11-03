"""Runs killed without a trace must not show "running" for ever (discovery runs 183 and 184, 28 Oct 2026)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session

from jobpilot.api.main import create_app
from jobpilot.db.models import AgentName, AgentRun, RunStatus
from jobpilot.db.session import get_engine
from jobpilot.db.stuck_runs import fail_stuck_runs

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def add_run(db_url, hours_ago, status=RunStatus.running, example=False, agent=AgentName.discovery):
    with Session(get_engine(db_url)) as s:
        run = AgentRun(agent=agent, status=status, is_example=example, log="discovery run started",
                       started_at=NOW - timedelta(hours=hours_ago))
        s.add(run)
        s.commit()
        return run.id


def row(db_url, run_id):
    with Session(get_engine(db_url)) as s:
        return s.get(AgentRun, run_id)


def test_a_run_stuck_for_over_24_hours_is_marked_failed_with_a_note(db_url):
    stuck = add_run(db_url, 24.5)
    assert fail_stuck_runs(db_url, now=NOW) == [stuck]
    run = row(db_url, stuck)
    assert run.status == RunStatus.error and run.finished_at is not None
    assert "still 'running' after 24 hours" in run.error and run.log.startswith("discovery run started\n")


def test_recent_finished_and_demo_runs_are_left_alone(db_url):
    recent, done, failed = add_run(db_url, 23), add_run(db_url, 100, RunStatus.ok), add_run(db_url, 100, RunStatus.error)
    demo = add_run(db_url, 100, example=True)
    assert fail_stuck_runs(db_url, now=NOW) == []
    assert row(db_url, recent).status == RunStatus.running and row(db_url, demo).status == RunStatus.running
    assert row(db_url, done).status == RunStatus.ok and row(db_url, failed).error == ""


def test_every_agent_counts_and_a_second_pass_changes_nothing(db_url):
    ids = [add_run(db_url, 48, agent=a) for a in (AgentName.discovery, AgentName.scoring, AgentName.documents, AgentName.apply)]
    assert sorted(fail_stuck_runs(db_url, now=NOW)) == sorted(ids)
    assert fail_stuck_runs(db_url, now=NOW) == []


def test_the_api_checks_for_stuck_runs_when_it_starts(db_url):
    stuck = add_run(db_url, 2000)            # long before the real clock's "now"
    live = add_run(db_url, 0)
    row_now = datetime.now(timezone.utc)
    with Session(get_engine(db_url)) as s:   # started_at of the live one is relative to the real clock for this test
        s.get(AgentRun, live).started_at = row_now
        s.commit()
    with TestClient(create_app(db_url, test_mode=True)):
        pass
    assert row(db_url, stuck).status == RunStatus.error and row(db_url, live).status == RunStatus.running


def test_a_failing_check_does_not_stop_the_api_from_starting(db_url, monkeypatch):
    import jobpilot.api.main as main

    def boom(*_a, **_k):
        raise RuntimeError("no such table")

    monkeypatch.setattr(main, "fail_stuck_runs", boom)
    with TestClient(create_app(db_url, test_mode=True), headers={"X-JobPilot-Client": "dashboard"}) as client:
        assert client.get("/api/meta").status_code == 200
