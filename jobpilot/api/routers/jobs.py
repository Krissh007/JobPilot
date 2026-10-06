"""Jobs: table/board listing, drawer detail, status moves (with history), notes, docs."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, nulls_last, text
from sqlmodel import Session, select

from jobpilot.api.deps import get_or_404, get_session
from jobpilot.db.models import Actor, Application, Job, JobStatus, StatusHistory
from jobpilot.db.repo import find_duplicate, set_status, upsert_job

router = APIRouter(tags=["jobs"])

SORTABLE = {"score": Job.score, "found_at": Job.found_at, "title": Job.title, "company": Job.company,
            "location": Job.location, "source": Job.source, "status": Job.status, "legacy_score": Job.legacy_score}
LIST_FIELDS = ("id", "title", "company", "source", "location", "exp_min", "exp_max", "score", "score_skills",
               "score_experience", "score_domain", "legacy_score", "status", "found_at", "applied_at", "is_example", "skills")


def fts_query(q: str) -> str:
    """User text -> safe FTS5 query: every word must match, as a prefix ("eval" finds "evaluation")."""
    return " AND ".join(f'"{token}"*' for token in re.findall(r"\w+", q))


def summary(job: Job) -> dict[str, Any]:
    data = {name: getattr(job, name) for name in LIST_FIELDS}
    data["has_docs"] = bool(job.resume_path or job.cover_letter_path)
    return data


@router.get("/jobs")
def list_jobs(
    session: Session = Depends(get_session),
    status: Optional[list[JobStatus]] = Query(None),
    source: Optional[str] = None,
    min_score: Optional[float] = None,
    q: Optional[str] = None,
    include_examples: bool = True,
    sort: str = "score",
    order: Literal["asc", "desc"] = "desc",
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    if sort not in SORTABLE:
        raise HTTPException(422, f"sort must be one of {sorted(SORTABLE)}")
    query = select(Job)
    if status:
        query = query.where(Job.status.in_(status))
    if source:
        query = query.where(Job.source == source.lower())
    if min_score is not None:
        query = query.where(Job.score >= min_score)
    if not include_examples:
        query = query.where(Job.is_example.is_(False))
    if q and q.strip():
        match = fts_query(q)
        if match:
            ids = text("SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH :m").bindparams(m=match)
            query = query.where(Job.id.in_(ids))

    total = session.exec(select(func.count()).select_from(query.subquery())).one()
    column = SORTABLE[sort]
    ordered = nulls_last(column.desc() if order == "desc" else column.asc())
    rows = session.exec(query.order_by(ordered, Job.id.desc()).offset(offset).limit(limit)).all()
    return {"total": total, "items": [summary(job) for job in rows]}


class JobCreate(BaseModel):
    url: str = Field(min_length=8)
    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    source: str = "manual"
    location: str = ""
    exp_min: Optional[int] = None
    exp_max: Optional[int] = None
    jd_text: str = ""
    notes: str = ""


@router.post("/jobs", status_code=201)
def create_job(body: JobCreate, session: Session = Depends(get_session)) -> dict[str, Any]:
    duplicate = find_duplicate(session, body.url, body.company, body.title, body.location, body.source)
    if duplicate is not None:
        raise HTTPException(409, {"message": "This job is already in the pipeline", "job_id": duplicate.id})
    job, _ = upsert_job(session, body.model_dump(), actor=Actor.user)
    session.commit()
    return summary(job)


def docs_folder(job: Job) -> Path | None:
    for value in (job.resume_path, job.cover_letter_path):
        if value:
            return Path(value).parent
    return None


@router.get("/jobs/{job_id}")
def get_job(job_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    job = get_or_404(session, Job, job_id)
    folder = docs_folder(job)
    apps = session.exec(select(Application).where(Application.job_id == job_id).order_by(Application.id.desc())).all()
    return {
        **job.model_dump(),
        "has_docs": folder is not None,
        "docs": {
            "resume": bool(job.resume_path) and Path(job.resume_path).is_file(),
            "cover": bool(job.cover_letter_path) and Path(job.cover_letter_path).is_file(),
            "report": folder is not None and (folder / "match_report.md").is_file(),
            "folder": str(folder) if folder else None,
        },
        "applications": [{"id": a.id, "state": a.state, "created_at": a.created_at, "decided_at": a.decided_at} for a in apps],
        "history": history(job_id, session),
    }


class JobPatch(BaseModel):
    status: Optional[JobStatus] = None
    notes: Optional[str] = None


@router.patch("/jobs/{job_id}")
def update_job(job_id: int, body: JobPatch, session: Session = Depends(get_session)) -> dict[str, Any]:
    job = get_or_404(session, Job, job_id)
    if body.status is not None:
        set_status(session, job, body.status, actor=Actor.user)
    if body.notes is not None:
        job.notes = body.notes
        session.add(job)
    session.commit()
    session.refresh(job)
    return summary(job) | {"notes": job.notes}


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: int, session: Session = Depends(get_session)) -> None:
    session.delete(get_or_404(session, Job, job_id))  # history, applications and fields cascade
    session.commit()


@router.get("/jobs/{job_id}/history")
def history(job_id: int, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = session.exec(select(StatusHistory).where(StatusHistory.job_id == job_id)
                        .order_by(StatusHistory.changed_at, StatusHistory.id)).all()
    return [{"from_status": h.from_status, "to_status": h.to_status, "changed_at": h.changed_at, "actor": h.actor} for h in rows]


DOC_KINDS = {"resume": "resume_path", "cover": "cover_letter_path"}


@router.get("/jobs/{job_id}/docs/{kind}")
def download_doc(job_id: int, kind: Literal["resume", "cover", "report"], session: Session = Depends(get_session)):
    job = get_or_404(session, Job, job_id)
    if kind == "report":
        folder = docs_folder(job)
        path = folder / "match_report.md" if folder else None
    else:
        value = getattr(job, DOC_KINDS[kind])
        path = Path(value) if value else None
    if path is None or not path.is_file():
        raise HTTPException(404, f"No {kind} document for job {job_id} yet")
    return FileResponse(path, filename=path.name)


@router.post("/jobs/{job_id}/open-folder")
def open_folder(job_id: int, session: Session = Depends(get_session)) -> dict[str, str]:
    folder = docs_folder(get_or_404(session, Job, job_id))
    if folder is None or not folder.is_dir():
        raise HTTPException(404, "No documents folder for this job yet")
    os.startfile(folder)  # type: ignore[attr-defined]  # Windows Explorer, on this PC only
    return {"opened": str(folder)}
