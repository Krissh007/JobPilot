"""
End-to-end smoke test of the built dashboard (web/dist) served by the real API on a
temporary seeded database, driven with headless installed Chrome. It clicks through every
screen, opens a job, moves it, and checks nothing logs a console error.

    cd web; npm run build      # once, or after UI changes
    .venv\\Scripts\\python -m pytest tests/test_ui_smoke.py
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
from sqlmodel import Session, select

from jobpilot.api import main as api_main
from jobpilot.api.routers import schedule as schedule_router
from jobpilot.db.models import Actor, Job, JobStatus, StatusHistory
from jobpilot.db.seed import seed
from jobpilot.db.session import get_engine
from jobpilot.scheduled import TASKS

pytest.importorskip("playwright")
uvicorn = pytest.importorskip("uvicorn")

if not (api_main.WEB_DIST / "index.html").is_file():
    pytest.skip("dashboard not built: run `npm run build` in web/", allow_module_level=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(db_url, monkeypatch):
    with Session(get_engine(db_url)) as s:
        seed(s)
        s.commit()
    # Task Scheduler is the user's real machine state: keep the test hermetic.
    tasks = json.dumps([{"name": name, "registered": False} for name in TASKS.values()])
    monkeypatch.setattr(schedule_router, "powershell", lambda script, timeout=60: tasks)
    monkeypatch.setitem(schedule_router._cache, "tasks", None)
    port = free_port()
    config = uvicorn.Config(api_main.create_app(db_url), host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def page(server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as error:
            pytest.skip(f"installed Chrome not available: {error}")
        pg = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(server)
        pg.errors = errors
        yield pg
        browser.close()


def tab(pg, name: str) -> None:
    pg.get_by_role("tab", name=name).click()
    pg.wait_for_timeout(400)


def test_every_screen_loads_and_a_job_can_be_moved(page, db_url):
    pg = page
    pg.get_by_role("region", name="Pipeline").wait_for()
    assert "Found" in pg.get_by_role("region", name="Pipeline").inner_text()

    # Board -> open a card -> drawer -> move it to Shortlisted from the status select
    tab(pg, "Board")
    with Session(get_engine(db_url)) as s:
        job = s.exec(select(Job).where(Job.status == JobStatus.found, Job.is_example)).first()
    pg.get_by_role("region", name="Found").get_by_text(job.title, exact=False).first.click()
    pg.get_by_role("heading", name=job.title).wait_for()
    pg.locator("select#status").select_option("shortlisted")
    pg.get_by_text("Moved to Shortlisted").wait_for()  # the toast names the new stage (was "Moved to Found")
    pg.keyboard.press("Escape")
    with Session(get_engine(db_url)) as s:
        assert s.get(Job, job.id).status == JobStatus.shortlisted
        last = s.exec(select(StatusHistory).where(StatusHistory.job_id == job.id).order_by(StatusHistory.id.desc())).first()
        assert (last.to_status, last.actor) == (JobStatus.shortlisted, Actor.user)

    tab(pg, "Jobs")
    assert pg.locator("table tbody tr").count() >= 10

    tab(pg, "Review")
    pg.get_by_role("button", name="Skip").first.wait_for()

    tab(pg, "Runs")
    pg.get_by_text("Search", exact=True).first.wait_for()

    tab(pg, "Answers")
    assert pg.locator("table tbody tr").count() >= 1

    tab(pg, "Analytics")
    pg.locator("label", has_text="Include example rows").locator("input").check()
    pg.get_by_text("Pipeline funnel").wait_for()
    hit = pg.locator("rect[aria-label^='score']").first
    hit.hover()
    fit = pg.locator("section", has_text="Fit scores")
    assert "job" in fit.locator("[role=status]").first.inner_text()  # the chart tooltip, not the weekly banner
    pg.get_by_role("button", name="Table").first.click()
    assert pg.locator("table tbody tr").count() == 6  # funnel stages

    tab(pg, "Database")
    pg.get_by_text("jobs", exact=False).first.wait_for()

    tab(pg, "Settings")
    pg.get_by_text("Windows task").wait_for()  # Schedule panel, Task Scheduler mocked
    pg.get_by_text("not registered").first.wait_for()

    # theme toggle cycles system -> light -> dark
    toggle = pg.get_by_role("button", name="Theme: system")
    toggle.click()
    pg.get_by_role("button", name="Theme: light").click()
    assert pg.evaluate("document.documentElement.dataset.theme") == "dark"

    assert pg.errors == []


def test_phone_width_has_no_horizontal_scroll(page):
    pg = page
    pg.set_viewport_size({"width": 390, "height": 900})
    for name in ("Board", "Jobs", "Review", "Analytics", "Settings"):
        tab(pg, name)
        assert pg.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0, name
    assert pg.errors == []
