"""Analytics: stage funnel (from status_history), score histogram, applications per day."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from jobpilot.api.deps import get_session, ist_date, today_ist
from jobpilot.db.models import Job, JobStatus, StatusHistory
from jobpilot.db.repo import get_setting

router = APIRouter(prefix="/analytics", tags=["analytics"])

PIPELINE = [JobStatus.found, JobStatus.shortlisted, JobStatus.docs_ready, JobStatus.applied,
            JobStatus.interview, JobStatus.offer]
RANK = {stage: i for i, stage in enumerate(PIPELINE)}


def _jobs(session: Session, include_examples: bool) -> list[Job]:
    query = select(Job)
    if not include_examples:
        query = query.where(Job.is_example.is_(False))
    return list(session.exec(query))


@router.get("/funnel")
def funnel(include_examples: bool = True, session: Session = Depends(get_session)) -> dict[str, Any]:
    """How many jobs ever reached each stage. A job imported straight into 'applied' counts as
    having passed the earlier stages too, so the funnel never widens."""
    jobs = {job.id: job for job in _jobs(session, include_examples)}
    furthest = {job_id: RANK.get(job.status, 0) for job_id, job in jobs.items()}
    for h in session.exec(select(StatusHistory).where(StatusHistory.job_id.in_(list(jobs)))):
        if h.to_status in RANK:
            furthest[h.job_id] = max(furthest[h.job_id], RANK[h.to_status])
    stages = [{"stage": stage.value, "count": sum(rank >= RANK[stage] for rank in furthest.values())} for stage in PIPELINE]
    rejected = sum(job.status == JobStatus.rejected for job in jobs.values())
    return {"stages": stages, "rejected": rejected, "total": len(jobs)}


@router.get("/score-histogram")
def score_histogram(include_examples: bool = True, session: Session = Depends(get_session)) -> dict[str, Any]:
    bins = [{"from": low, "to": low + 9 if low < 90 else 100, "count": 0} for low in range(0, 100, 10)]
    unscored = 0
    for job in _jobs(session, include_examples):
        if job.score is None:
            unscored += 1
        else:
            bins[min(int(job.score // 10), 9)]["count"] += 1
    return {"bins": bins, "unscored": unscored, "threshold": get_setting(session, "threshold")}


@router.get("/applications-per-day")
def applications_per_day(days: int = Query(30, ge=1, le=365), include_examples: bool = True,
                         session: Session = Depends(get_session)) -> dict[str, Any]:
    today = today_ist()
    counts = {today - timedelta(days=offset): 0 for offset in range(days - 1, -1, -1)}
    for job in _jobs(session, include_examples):
        day = ist_date(job.applied_at)
        if day in counts:
            counts[day] += 1
    return {"days": [{"date": day.isoformat(), "count": count} for day, count in counts.items()]}
