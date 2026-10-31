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
                                         "window_minutes": 840, "weekly_day": "mon", "weekly_time": "09:00",
                                         "days": scheduled.ALL_DAYS, "times": []}


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


# ---- days of the week, explicit run times ----------------------------

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]
TIMES = {**CFG, "times": ["09:30", "18:00"]}


def test_calendar_assumption():
    assert ist(9, 0, day=12).strftime("%a") == "Mon" and ist(9, 0, day=10).strftime("%a") == "Sat"


def test_default_steps_are_discovery_scoring_documents():
    from jobpilot.agents import discover, documents, shortlist

    assert scheduled.default_steps() == [("discovery", discover.run), ("scoring", shortlist.run), ("documents", documents.run)]


def test_weekdays_only_blocks_the_weekend_for_late_runs_and_catch_up():
    cfg = {**CFG, "days": WEEKDAYS}
    assert scheduled.late_run_ok(ist(9, 0, day=12), cfg) and not scheduled.late_run_ok(ist(9, 0, day=10), cfg)
    assert scheduled.catch_up_due(ist(14, 13, day=12), cfg, None) and not scheduled.catch_up_due(ist(14, 13, day=10), cfg, None)


def test_default_days_means_every_day():
    assert scheduled.late_run_ok(ist(9, 0, day=10), CFG) and scheduled.late_run_ok(ist(9, 0, day=12), CFG)


def test_interval_slots_match_the_task_scheduler_repetition():
    assert scheduled.run_slots(CFG) == [8 * 60, 12 * 60, 16 * 60, 20 * 60]
    assert scheduled.run_slots(TIMES) == [9 * 60 + 30, 18 * 60]


def test_explicit_times_late_run_only_near_a_slot():
    assert scheduled.late_run_ok(ist(9, 30, day=12), TIMES) and scheduled.late_run_ok(ist(10, 45, day=12), TIMES)
    assert not scheduled.late_run_ok(ist(12, 0, day=12), TIMES)     # between slots
    assert not scheduled.late_run_ok(ist(9, 0, day=12), TIMES)      # before the first slot


def test_explicit_times_catch_up():
    asleep = ist(18, 0, day=11).astimezone(timezone.utc)
    assert scheduled.catch_up_due(ist(10, 5, day=12), TIMES, asleep)              # missed 09:30
    done = ist(9, 31, day=12).astimezone(timezone.utc)
    assert not scheduled.catch_up_due(ist(10, 5, day=12), TIMES, done)            # that slot already ran
    assert not scheduled.catch_up_due(ist(14, 0, day=12), TIMES, asleep)          # 09:30 is over 4 h ago
    assert scheduled.catch_up_due(ist(18, 20, day=12), TIMES, done)               # missed 18:00
    assert scheduled.catch_up_due(ist(9, 40, day=12), TIMES, None)


def test_config_carries_days_and_times(session):
    from jobpilot.db.repo import put_setting

    put_setting(session, "schedule", {"every_hours": 6, "start": "07:00", "end": "19:00", "timezone": "Asia/Kolkata",
                                      "days": ["fri", "mon"], "times": ["18:00", "09:30", "09:30"]})
    cfg = scheduled.config(session)
    assert cfg["days"] == ["mon", "fri"] and cfg["times"] == ["09:30", "18:00"]


def test_pipeline_log_line_names_the_steps(tmp_path, capsys):
    scheduled.run_pipeline([("discover", lambda: None), ("shortlist", lambda: None)], lock=tmp_path / "l")
    assert "scheduled pass started (discover, shortlist)" in capsys.readouterr().out


# ---- settings API: the filters you can set -------------------------------------------

def test_settings_api_exposes_and_validates_schedule_and_filters(client):
    body = client.get("/api/settings").json()
    assert body["schedule"]["days"] == scheduled.ALL_DAYS and body["schedule"]["times"] == []
    assert body["discovery"]["max_posted_days"] == 7 and body["discovery"]["locations"] == ["Hyderabad"]
    assert body["scoring"] == {"exp_min": 3, "exp_max": 7, "avoid_title_words": []}

    saved = client.put("/api/settings", json={
        "schedule": {"every_hours": 4, "start": "08:00", "end": "22:00", "days": ["sat", "mon", "tue"], "times": ["18:00", "09:30"]},
        "discovery": {"locations": ["Hyderabad", "Pune"], "max_posted_days": 3, "enrich_per_query": 5},
        "scoring": {"exp_max": 5}}).json()
    assert saved["schedule"]["days"] == ["mon", "tue", "sat"]
    assert saved["schedule"]["times"] == ["09:30", "18:00"]
    assert saved["discovery"]["locations"] == ["Hyderabad", "Pune"] and saved["discovery"]["max_posted_days"] == 3
    assert saved["discovery"]["enrich_per_query"] == 5 and saved["discovery"]["use_variants"] is True   # untouched keys stay
    assert saved["scoring"] == {"exp_min": 3, "exp_max": 5, "avoid_title_words": []}

    # a second partial patch keeps the first
    again = client.put("/api/settings", json={"discovery": {"max_posted_days": 14}}).json()
    assert again["discovery"]["locations"] == ["Hyderabad", "Pune"] and again["discovery"]["max_posted_days"] == 14


@pytest.mark.parametrize("payload", [
    {"auto_submit": "maybe"},
    {"schedule": {"every_hours": 4, "start": "08:00", "end": "22:00", "days": []}},
    {"schedule": {"every_hours": 4, "start": "08:00", "end": "22:00", "days": ["funday"]}},
    {"schedule": {"every_hours": 4, "start": "08:00", "end": "22:00", "times": ["25:00"]}},
    {"schedule": {"every_hours": 4, "start": "8:00", "end": "22:00"}},
    {"discovery": {"max_posted_days": 0}},
    {"discovery": {"sources": ["monster"]}},
    {"scoring": {"exp_min": 9}},
])
def test_settings_api_rejects_bad_values(client, payload):
    assert client.put("/api/settings", json=payload).status_code == 422
    assert client.get("/api/settings").json()["schedule"]["days"] == scheduled.ALL_DAYS     # nothing was half-saved


def apply_review_only_flag(monkeypatch, steps) -> bool:
    """Run only the apply step with runner.run replaced by a spy; returns the review_only it was called with."""
    from jobpilot.agents.apply import runner
    seen = []
    monkeypatch.setattr(runner, "run", lambda **kwargs: seen.append(kwargs["review_only"]))
    dict(steps)["apply"]()
    return seen[0]


def test_run_now_runs_the_four_agents_in_order_with_apply_review_only(monkeypatch):  # JPA-TC-006
    steps = scheduled.run_now_steps()
    assert [name for name, _ in steps] == ["discovery", "scoring", "documents", "apply"]
    assert apply_review_only_flag(monkeypatch, steps) is True                       # review-only by default
    assert apply_review_only_flag(monkeypatch, scheduled.run_now_steps(review_only=False)) is False


@pytest.mark.parametrize("argv, review_only", [(["--now"], True), (["--now", "--review-only"], True),
                                               (["--now", "--auto-submit"], False),
                                               (["--now", "--auto-submit", "--review-only"], True)])
def test_now_is_review_only_unless_auto_submit_is_explicit(monkeypatch, argv, review_only):
    asked = []
    monkeypatch.setattr(scheduled, "run_now_steps", lambda review_only=True: asked.append(review_only) or [])
    monkeypatch.setattr(scheduled, "run_pipeline", lambda steps=None, lock=None: 0)
    with pytest.raises(SystemExit):
        scheduled.main(argv)
    assert asked == [review_only]


def test_auto_submit_flag_needs_now(capsys):
    with pytest.raises(SystemExit) as exit_info:
        scheduled.main(["--auto-submit"])
    assert exit_info.value.code == 2
