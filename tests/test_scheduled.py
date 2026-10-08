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
    assert [t["registered"] for t in body["tasks"]] == [False, False] and "register_tasks.ps1" in body["command"]

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
