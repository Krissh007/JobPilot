from __future__ import annotations

import json
import os

import pytest

from fastapi.testclient import TestClient

from jobpilot import scheduled
from jobpilot.api.main import create_app
from jobpilot.api.routers import schedule as schedule_router


@pytest.fixture
def client(db_url):
    return TestClient(create_app(db_url))


def test_pipeline_runs_every_step_even_after_a_failure(tmp_path, capsys):
    calls = []

    def boom():
        calls.append("search")
        raise RuntimeError("LinkedIn timed out")

    lock = tmp_path / "scheduled.lock"
    code = scheduled.run_pipeline([("search", boom), ("score", lambda: calls.append("score")),
                                   ("docs", lambda: calls.append("docs"))], lock=lock)
    assert calls == ["search", "score", "docs"] and code == 1
    assert "search failed: LinkedIn timed out" in capsys.readouterr().out
    assert not lock.exists()  # released


def test_lock_skips_an_overlapping_pass(tmp_path):
    lock = tmp_path / "scheduled.lock"
    lock.write_text(str(os.getppid()))  # a live process (pytest's parent) holds it
    ran = []
    assert scheduled.run_pipeline([("search", lambda: ran.append(1))], lock=lock) == 0
    assert ran == [] and lock.exists()

    lock.write_text("999999")  # stale lock from a crashed pass is taken over
    assert scheduled.run_pipeline([("search", lambda: ran.append(1))], lock=lock) == 0 and ran == [1]


def test_config_matches_settings(session):
    assert scheduled.config(session) == {"tasks": scheduled.TASKS, "start": "08:00", "every_hours": 4,
                                         "window_minutes": 840, "weekly_day": "mon", "weekly_time": "09:00"}


def test_schedule_api_reports_tasks_and_registers(client: TestClient, monkeypatch):
    scripts = []
    registered = {"on": False}

    def fake_powershell(script, timeout=60):
        scripts.append(script)
        if "register_tasks.ps1" in script:
            registered["on"] = "-Unregister" not in script
            return "Registered: JobPilot Search-Score-Docs\n"
        if not registered["on"]:
            return json.dumps([{"name": n, "registered": False} for n in scheduled.TASKS.values()])
        return json.dumps([{"name": n, "registered": True, "state": "Ready", "next_run": "2026-10-08T12:00:00+05:30",
                            "last_run": None, "last_result": 267011} for n in scheduled.TASKS.values()])

    monkeypatch.setattr(schedule_router, "powershell", fake_powershell)
    monkeypatch.setitem(schedule_router._cache, "tasks", None)
    body = client.get("/api/schedule").json()
    assert [t["registered"] for t in body["tasks"]] == [False] * len(scheduled.TASKS) and "register_tasks.ps1" in body["command"]

    body = client.post("/api/schedule/register").json()
    assert body["output"] == ["Registered: JobPilot Search-Score-Docs"]
    assert body["tasks"][0]["state"] == "Ready" and body["tasks"][0]["last_result_text"] == "not run yet"

    client.post("/api/schedule/unregister")
    assert any("-Unregister" in s for s in scripts) and not client.get("/api/schedule").json()["tasks"][0]["registered"]


def test_schedule_api_survives_task_scheduler_errors(client: TestClient, monkeypatch):
    def broken(script, timeout=60):
        raise RuntimeError("Access is denied")

    monkeypatch.setattr(schedule_router, "powershell", broken)
    monkeypatch.setitem(schedule_router._cache, "tasks", None)
    body = client.get("/api/schedule").json()
    assert body["tasks"] == [] and "Access is denied" in body["error"]


def test_late_runs_outside_the_window_are_skipped():
    from datetime import datetime

    at = lambda h, m: datetime(2026, 10, 8, h, m)  # noqa: E731
    assert scheduled.within_window(at(8, 0), "08:00", 840) and scheduled.within_window(at(20, 1), "08:00", 840)
    assert scheduled.within_window(at(22, 25), "08:00", 840)  # missed 20:00 run started a bit late
    assert not scheduled.within_window(at(23, 49), "08:00", 840) and not scheduled.within_window(at(7, 30), "08:00", 840)


# ---- catch-up after sleep (10 Oct 2026: Windows skipped 7 runs while the PC slept) ----

from datetime import datetime, timedelta, timezone  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
CFG = {"start": "08:00", "every_hours": 4, "window_minutes": 840}


def ist(hour, minute=0, day=10):
    return datetime(2026, 10, day, hour, minute, tzinfo=IST)


def test_catch_up_runs_when_the_last_search_is_overdue():
    last = ist(0, 0, day=9).astimezone(timezone.utc)  # slept through the whole of 9 Oct
    assert scheduled.catch_up_due(ist(14, 13), CFG, last)
    assert scheduled.catch_up_due(ist(9, 0), CFG, None)  # never searched


def test_catch_up_skips_a_recent_search_and_outside_the_window():
    recent = ist(12, 5).astimezone(timezone.utc)
    assert not scheduled.catch_up_due(ist(14, 0), CFG, recent)       # under 3.5 h ago
    assert scheduled.catch_up_due(ist(15, 40), CFG, recent)          # 3 h 35 m: due
    old = ist(8, 0, day=9).astimezone(timezone.utc)
    assert not scheduled.catch_up_due(ist(7, 30), CFG, old)          # before 08:00
    assert not scheduled.catch_up_due(ist(22, 45), CFG, old)         # after 22:30 (window + grace)


def test_last_search_started_ignores_other_agents_and_examples(session):
    from jobpilot.db.models import AgentName, AgentRun

    session.add(AgentRun(agent=AgentName.search, started_at=datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)))
    session.add(AgentRun(agent=AgentName.score, started_at=datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)))
    session.add(AgentRun(agent=AgentName.search, is_example=True,
                         started_at=datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)))
    session.commit()
    assert scheduled.last_search_started(session) == datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)


def test_catch_up_exits_quietly_when_nothing_is_due(monkeypatch):
    monkeypatch.setattr(scheduled, "config", lambda session=None: {**CFG, "tasks": scheduled.TASKS})
    monkeypatch.setattr(scheduled, "last_search_started", lambda session=None: datetime.now(timezone.utc))
    ran = []
    monkeypatch.setattr(scheduled, "run_pipeline", lambda *a, **k: ran.append(1) or 0)
    with pytest.raises(SystemExit) as exit_info:
        scheduled.main(["--catch-up"])
    assert exit_info.value.code == 0 and ran == []
