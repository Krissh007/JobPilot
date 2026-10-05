from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlmodel import func, select

from jobpilot.db.models import (
    Actor,
    AgentRun,
    Answer,
    Application,
    ApplicationField,
    Job,
    JobStatus,
    StatusHistory,
)
from jobpilot.db.repo import get_setting, put_setting, set_status, upsert_job
from jobpilot.db.seed import EXAMPLE_JOBS, purge, seed


def job_data(**overrides):
    data = {
        "title": "AI Business Analyst", "company": "Acme", "source": "linkedin", "location": "Hyderabad",
        "url": "https://www.linkedin.com/jobs/view/4012345678/?trk=x", "jd_text": "Own LLM requirements and SQL analysis.",
    }
    data.update(overrides)
    return data


def count(session, model):
    return session.exec(select(func.count()).select_from(model)).one()


def test_pragmas_wal_and_foreign_keys(session):
    assert session.exec(text("PRAGMA journal_mode")).scalar() == "wal"
    assert session.exec(text("PRAGMA foreign_keys")).scalar() == 1


def test_default_settings_from_migration(session):
    assert get_setting(session, "threshold") == 60
    assert get_setting(session, "daily_apply_cap") == 10
    put_setting(session, "threshold", 70)
    session.commit()
    assert get_setting(session, "threshold") == 70


def test_upsert_inserts_with_history(session):
    job, created = upsert_job(session, job_data())
    session.commit()
    assert created and job.status == JobStatus.found
    assert job.url_canonical == "https://linkedin.com/jobs/view/4012345678"
    history = session.exec(select(StatusHistory)).all()
    assert [(h.from_status, h.to_status, h.actor) for h in history] == [(None, JobStatus.found, Actor.agent)]


def test_upsert_dedupes_by_canonical_url_and_keeps_user_fields(session):
    job, _ = upsert_job(session, job_data())
    set_status(session, job, JobStatus.shortlisted)
    job.notes = "call recruiter"
    session.commit()

    again, created = upsert_job(session, job_data(url="https://in.linkedin.com/jobs/view/ai-ba-4012345678?refId=1",
                                                  jd_text="A much longer description " * 10))
    session.commit()
    assert not created and again.id == job.id
    assert again.status == JobStatus.shortlisted and again.notes == "call recruiter"
    assert again.jd_text.startswith("A much longer")
    assert count(session, Job) == 1


def test_upsert_falls_back_to_hash_across_portals(session):
    upsert_job(session, job_data())
    _, created = upsert_job(session, job_data(url="https://www.naukri.com/job-listings-x-150926500123", source="naukri",
                                              company="ACME Pvt Ltd", location="Hyderabad, Telangana"))
    session.commit()
    assert not created and count(session, Job) == 1


def test_same_portal_different_job_ids_stay_separate(session):
    upsert_job(session, job_data(url="https://www.naukri.com/job-listings-ba-150926500001", source="naukri"))
    _, created = upsert_job(session, job_data(url="https://www.naukri.com/job-listings-ba-150926500002", source="naukri"))
    session.commit()
    assert created and count(session, Job) == 2


def test_set_status_records_history_and_applied_at(session):
    job, _ = upsert_job(session, job_data())
    assert set_status(session, job, "applied", actor=Actor.user)
    assert not set_status(session, job, "applied")
    session.commit()
    assert job.applied_at is not None
    assert [h.to_status for h in session.exec(select(StatusHistory).order_by(StatusHistory.id))] == [
        JobStatus.found, JobStatus.applied]


def test_status_enum_enforced_by_database(session):
    upsert_job(session, job_data())
    session.commit()
    with pytest.raises(IntegrityError):
        session.exec(text("UPDATE jobs SET status = 'ghosted'"))
    session.rollback()
    with pytest.raises((StatementError, LookupError, ValueError)):
        set_status(session, session.exec(select(Job)).one(), "ghosted")


def test_url_unique(session):
    upsert_job(session, job_data())
    session.commit()
    session.add(Job(title="x", company="y", url=job_data()["url"], url_canonical="z", dedupe_hash="h"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_fts_tracks_insert_update_delete(session):
    job, _ = upsert_job(session, job_data(jd_text="Define evaluation metrics for retrieval augmented generation"))
    session.commit()

    def hits(q):
        return session.exec(text("SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH :q").bindparams(q=q)).scalars().all()

    assert hits("retrieval") == [job.id]
    assert hits("evaluate") == [job.id]  # porter stemming: evaluate ~ evaluation
    job.jd_text = "Forecasting demand with merchandising teams"
    session.add(job)
    session.commit()
    assert hits("retrieval") == [] and hits("forecasting") == [job.id]
    session.delete(job)
    session.commit()
    assert hits("forecasting") == []


def test_seed_then_purge_keeps_real_rows(session):
    real, _ = upsert_job(session, job_data())
    session.commit()

    assert seed(session) == len(EXAMPLE_JOBS) == 15
    session.commit()
    assert seed(session) == 0  # idempotent
    assert count(session, Job) == 16
    assert count(session, Application) == 2 and count(session, ApplicationField) == 4
    assert count(session, AgentRun) == 3 and count(session, Answer) == 6
    statuses = {j.status for j in session.exec(select(Job).where(Job.is_example))}
    assert statuses == set(JobStatus)  # every stage has at least one example

    purge(session)
    session.commit()
    assert [j.id for j in session.exec(select(Job))] == [real.id]
    assert count(session, ApplicationField) == 0  # cascaded
    assert count(session, StatusHistory) == 1  # only the real job's history
    assert count(session, AgentRun) == 0 and count(session, Answer) == 0
