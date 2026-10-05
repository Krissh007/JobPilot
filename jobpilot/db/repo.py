"""Shared write paths: every status change goes through set_status so history stays complete."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from jobpilot.db.dedupe import canonical_url, dedupe_hash
from jobpilot.db.models import DEFAULT_SETTINGS, Actor, Job, JobStatus, Setting, StatusHistory, utcnow

# Fields a re-import may refresh. status, notes, applied_at and docs belong to
# the user / later agents and are never overwritten by an upsert.
REFRESHABLE = ("title", "company", "source", "location", "exp_min", "exp_max", "legacy_score")
KEYS_SET_HERE = ("id", "status", "url", "url_canonical", "dedupe_hash")


def find_duplicate(session: Session, url: str, company: str, title: str, location: str, source: str = "") -> Job | None:
    canon = canonical_url(url)
    job = session.exec(select(Job).where(Job.url_canonical == canon)).first()
    if job is None:
        # The company+title+city fallback only catches the same opening on a
        # different portal. Within one portal a different job id is a different
        # opening (e.g. Accenture lists many "Business Analyst, Hyderabad" reqs).
        job = session.exec(
            select(Job).where(Job.dedupe_hash == dedupe_hash(company, title, location), Job.source != source)
        ).first()
    return job


def upsert_job(session: Session, data: dict[str, Any], actor: Actor = Actor.agent) -> tuple[Job, bool]:
    """Insert or refresh a job by dedupe key. Returns (job, created)."""
    url = str(data["url"]).strip()
    title, company, location = data.get("title", ""), data.get("company", ""), data.get("location", "")
    job = find_duplicate(session, url, company, title, location, str(data.get("source") or ""))

    if job is not None:
        for name in REFRESHABLE:
            value = data.get(name)
            if value not in (None, ""):
                setattr(job, name, value)
        jd = str(data.get("jd_text") or "")
        if len(jd) > len(job.jd_text or ""):
            job.jd_text = jd
        session.add(job)
        session.flush()
        return job, False

    status = JobStatus(data.get("status") or JobStatus.found)
    job = Job(
        **{k: v for k, v in data.items() if k in Job.model_fields and k not in KEYS_SET_HERE},
        url=url,
        url_canonical=canonical_url(url),
        dedupe_hash=dedupe_hash(company, title, location),
        status=status,
    )
    session.add(job)
    session.flush()
    session.add(StatusHistory(job_id=job.id, from_status=None, to_status=status, actor=actor))
    return job, True


def set_status(session: Session, job: Job, to: JobStatus | str, actor: Actor = Actor.user) -> bool:
    """Move a job to a new stage and record it. Returns False if nothing changed."""
    to = JobStatus(to)
    if job.status == to:
        return False
    session.add(StatusHistory(job_id=job.id, from_status=job.status, to_status=to, actor=actor))
    job.status = to
    if to == JobStatus.applied and job.applied_at is None:
        job.applied_at = utcnow()
    session.add(job)
    return True


def get_setting(session: Session, key: str) -> Any:
    row = session.get(Setting, key)
    return row.value if row is not None else DEFAULT_SETTINGS.get(key)


def put_setting(session: Session, key: str, value: Any) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        session.add(row)
