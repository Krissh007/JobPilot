from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook
from sqlmodel import Session, select

from jobpilot.agents import search
from jobpilot.agents._run import agent_run
from jobpilot.db.models import AgentName, AgentRun, Job, JobStatus, RunStatus
from jobpilot.db.session import get_engine
from jobpilot.legacy.importer import import_legacy, legacy_time, map_row, read_legacy_db

LEGACY_ROWS = [
    # job_url, title, company, source, status, score, discovered_at, experience_min, experience_max, description
    ("https://www.naukri.com/job-listings-ai-ba-acme-hyderabad-2-to-4-years-150926500001?src=x", "AI Business Analyst",
     "Acme", "naukri", "new", 72.5, "2026-10-01T09:30:00", 2, 4, "Own LLM product requirements. " * 20),
    ("https://www.linkedin.com/jobs/view/4000000002/?trk=y", "Product Analyst", "Beta", "linkedin", "applied", 64.0,
     "2026-09-28T10:00:00", None, None, "Experimentation and SQL."),
    ("https://www.naukri.com/job-listings-ba-gamma-150926500003", "Business Analyst", "Gamma", "naukri", "rejected", 30.0,
     "2026-09-28T10:00:00", None, None, "Walk-in, 8-12 years."),
    ("https://www.linkedin.com/jobs/view/4000000004/", "AI Analyst", "Delta", "linkedin", "interviewing", 80.0,
     "2026-09-20T08:00:00", 3, 5, "GenAI evaluation."),
]


def make_legacy(tmp_path: Path) -> Path:
    root = tmp_path / "legacy"
    (root / "data" / "exports").mkdir(parents=True)
    with sqlite3.connect(root / "data" / "jobs.db") as c:
        c.execute("""CREATE TABLE jobs (id INTEGER PRIMARY KEY, job_url TEXT UNIQUE, title TEXT, company TEXT,
                     source TEXT, location TEXT DEFAULT 'Hyderabad', status TEXT, match_score REAL, discovered_at TEXT,
                     experience_min INTEGER, experience_max INTEGER, description TEXT, notes TEXT, applied_at TEXT)""")
        c.executemany("""INSERT INTO jobs (job_url, title, company, source, status, match_score, discovered_at,
                         experience_min, experience_max, description) VALUES (?,?,?,?,?,?,?,?,?,?)""", LEGACY_ROWS)
    return root


def test_legacy_time_is_ist():
    assert legacy_time("2026-10-01T09:30:00") == datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)
    assert legacy_time("") is None and legacy_time("not a date") is None


def test_map_row_status_and_experience_text():
    job, _ = map_row({"job_url": "https://x/1", "title": "BA", "company": "C", "status": "interviewing",
                      "experience": "2-5 Yrs", "match_score": "61.5", "discovered_at": "2026-10-01T09:30:00"})
    assert job["status"] == JobStatus.interview and (job["exp_min"], job["exp_max"]) == (2, 5)
    assert job["legacy_score"] == 61.5 and job["applied_at"] == job["found_at"]
    assert map_row({"job_url": "https://x/2", "status": "rejected"}) == (None, "legacy rejected")
    assert map_row({"title": "no url"}) == (None, "no url")


def test_legacy_db_is_opened_read_only(tmp_path):
    root = make_legacy(tmp_path)
    assert len(read_legacy_db(root / "data" / "jobs.db")) == 4
    uri = f"file:{(root / 'data' / 'jobs.db').as_posix()}?mode=ro"
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(uri, uri=True).execute("DELETE FROM jobs")


def test_import_is_idempotent_and_preserves_jobpilot_status(tmp_path, session):
    root = make_legacy(tmp_path)
    first = import_legacy(session, root)
    session.commit()
    assert (first.seen, first.created, first.updated, first.skipped) == (4, 3, 0, 1)

    jobs = {j.company: j for j in session.exec(select(Job))}
    assert jobs["Acme"].status == JobStatus.found and jobs["Acme"].legacy_score == 72.5
    assert jobs["Acme"].url_canonical == "https://naukri.com/job-listings-150926500001"
    assert jobs["Beta"].status == JobStatus.applied and jobs["Beta"].applied_at is not None
    assert jobs["Delta"].status == JobStatus.interview

    jobs["Acme"].status = JobStatus.shortlisted  # changed in JobPilot
    session.add(jobs["Acme"])
    session.commit()
    second = import_legacy(session, root)
    session.commit()
    assert (second.created, second.updated) == (0, 3)
    assert session.exec(select(Job).where(Job.company == "Acme")).one().status == JobStatus.shortlisted


def test_excel_fallback_when_db_missing(tmp_path, session):
    root = tmp_path / "legacy"
    exports = root / "data" / "exports"
    exports.mkdir(parents=True)
    wb = Workbook()
    wb.active.append(["title", "company", "location", "experience", "source", "match_score", "job_url", "description", "discovered_at"])
    wb.active.append(["AI BA", "Epsilon", "Hyderabad", "3-6 Yrs", "naukri", 66, "https://www.naukri.com/job-listings-x-150926500009",
                      "JD text", "2026-10-02T08:00:00"])
    wb.save(exports / "new_leads_7d_2026-10-02.xlsx")
    (exports / "~$new_leads_7d_2026-10-03.xlsx").write_bytes(b"lock file")

    logged = []
    stats = import_legacy(session, root, log=logged.append)
    session.commit()
    assert stats.created == 1 and stats.source.endswith("new_leads_7d_2026-10-02.xlsx")
    assert "falling back" in logged[0]
    assert session.exec(select(Job)).one().exp_max == 6


def test_agent_run_records_ok_and_error(db_url):
    with agent_run(AgentName.score, db_url) as rec:
        rec.found, rec.processed = 2, 5
        rec.log("working")
    with pytest.raises(RuntimeError):
        with agent_run(AgentName.score, db_url) as rec:
            raise RuntimeError("quota exhausted")

    with Session(get_engine(db_url)) as s:
        ok, failed = s.exec(select(AgentRun).order_by(AgentRun.id)).all()
    assert ok.status == RunStatus.ok and (ok.found, ok.processed) == (2, 5) and "working" in ok.log
    assert ok.finished_at is not None
    assert failed.status == RunStatus.error and "quota exhausted" in failed.error


def test_search_import_legacy_db_end_to_end(tmp_path, db_url, monkeypatch):
    root = make_legacy(tmp_path)
    monkeypatch.setattr(search, "get_config", lambda: SimpleNamespace(legacy_agent_dir=root, legacy_python="python"))
    monkeypatch.setattr(search, "run_legacy_search", lambda *a: pytest.fail("browser search must be skipped"))
    search.run(import_legacy_db=True, db_url=db_url)
    with Session(get_engine(db_url)) as s:
        run = s.exec(select(AgentRun)).one()
    assert run.status == RunStatus.ok and (run.found, run.processed) == (3, 4)
    assert "3 new, 0 updated, 1 skipped (legacy rejected: 1)" in run.log


FAKE_SHIM = r"""
import json, sys
args = sys.argv
known = json.load(open(args[args.index("--known-urls") + 1], encoding="utf-8"))
kw = []
for a in args[args.index("--keywords") + 1:]:
    if a.startswith("--"): break
    kw.append(a)
print("INFO | naukri returned 2 jobs | keywords=" + "|".join(kw) + (" | easy-apply-only" if "--linkedin-easy-apply-only" in args else ""))
jobs = [
    {"job_url": "https://www.naukri.com/job-listings-ai-ba-zeta-150926500101", "title": "AI Business Analyst",
     "company": "Zeta", "source": "naukri", "location": "Hyderabad", "description": "LLM requirements " * 20,
     "match_score": 71.0, "status": "new", "experience_min": 2, "experience_max": 5},
    {"job_url": "https://www.linkedin.com/jobs/view/4100000002/", "title": "Product Analyst", "company": "Eta",
     "source": "linkedin", "location": "Hyderabad", "description": "Experiments", "match_score": 55.0, "status": "new"},
]
for job in jobs:
    if job["job_url"] not in known:
        print("JOBPILOT_JOB " + json.dumps(job))
print("JOBPILOT_RESULT " + json.dumps({"discovered": 9, "stale": 3, "rejected": 4, "matched": 2}))
"""


def test_search_streams_matches_into_jobpilot_db_only(tmp_path, db_url, monkeypatch):
    import sys
    shim = tmp_path / "fake_shim.py"
    shim.write_text(FAKE_SHIM, encoding="utf-8")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    monkeypatch.setattr(search, "SHIM", shim)
    monkeypatch.setattr(search, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(search, "get_config", lambda: SimpleNamespace(legacy_agent_dir=legacy, legacy_python=sys.executable))

    search.run(db_url=db_url)
    with Session(get_engine(db_url)) as s:
        jobs = {j.company: j for j in s.exec(select(Job))}
        run = s.exec(select(AgentRun)).one()
    assert set(jobs) == {"Zeta", "Eta"} and jobs["Zeta"].legacy_score == 71.0 and jobs["Zeta"].exp_max == 5
    assert jobs["Zeta"].status == JobStatus.found and len(jobs["Zeta"].jd_text) > 100
    assert (run.found, run.processed) == (2, 2)
    assert "[legacy] INFO | naukri returned 2 jobs | keywords=AI Business Analyst|AI Product Analyst|Business Analyst|Product Analyst | easy-apply-only" in run.log and "rejected by gates 4, matched 2" in run.log
    assert not list(legacy.iterdir())  # nothing written into the legacy project

    search.run(db_url=db_url)  # second run: JobPilot passes its URLs, so the shim skips them
    with Session(get_engine(db_url)) as s:
        last = s.exec(select(AgentRun).order_by(AgentRun.id.desc())).first()
    assert (last.found, last.processed) == (0, 0) and "Running legacy discovery (2 known URLs skipped; LinkedIn: Easy Apply jobs only)" in last.log
