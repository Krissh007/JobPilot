"""Pipeline meta (rail counts), CSV/JSON export, and example data seed/purge."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, text
from sqlmodel import Session, select

from jobpilot.api.deps import get_session, ist_date, today_ist
from jobpilot.db.models import (
    AgentRun,
    Answer,
    Application,
    ApplicationField,
    ApplicationState,
    Job,
    JobStatus,
    RunStatus,
    Setting,
    StatusHistory,
)
from jobpilot.db.repo import get_setting
from jobpilot.db.seed import purge, seed

router = APIRouter(tags=["data"])


@router.get("/meta")
def meta(session: Session = Depends(get_session)) -> dict[str, Any]:
    session.exec(text("SELECT 1"))
    rows = session.exec(select(Job.status, Job.is_example, Job.applied_at, Job.score)).all()
    stages = {status.value: 0 for status in JobStatus}
    for status, _, _, _ in rows:
        stages[status.value] += 1
    today = today_ist()

    def apps(state: ApplicationState) -> int:
        return session.exec(select(func.count()).select_from(Application).where(Application.state == state)).one()

    running = session.exec(select(AgentRun).where(AgentRun.status == RunStatus.running, AgentRun.is_example.is_(False))).all()
    return {
        "db": "ok",
        "total_jobs": len(rows),
        "example_jobs": sum(1 for row in rows if row[1]),
        "unscored": sum(1 for row in rows if row[3] is None),
        "stages": stages,
        "applied_today": sum(1 for row in rows if ist_date(row[2]) == today),
        "daily_apply_cap": get_setting(session, "daily_apply_cap"),
        "threshold": get_setting(session, "threshold"),
        "pending_review": apps(ApplicationState.pending_review),
        "blocked": apps(ApplicationState.blocked),
        "running": [{"id": r.id, "agent": r.agent.value, "started_at": r.started_at} for r in running],
        "today": today.isoformat(),
    }


CSV_FIELDS = ["id", "title", "company", "source", "location", "exp_min", "exp_max", "score", "score_skills",
              "score_experience", "score_domain", "legacy_score", "status", "url", "found_at", "applied_at",
              "resume_path", "cover_letter_path", "notes", "is_example", "score_reason"]


@router.get("/export/jobs.csv")
def export_csv(session: Session = Depends(get_session)) -> StreamingResponse:
    buffer = io.StringIO()
    buffer.write("﻿")  # BOM so Excel opens UTF-8 (₹, names) correctly
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for job in session.exec(select(Job).order_by(Job.id)):
        writer.writerow({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in job.model_dump().items()})
    stamp = today_ist().isoformat()
    return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="jobpilot_jobs_{stamp}.csv"'})


@router.get("/export/all.json")
def export_json(session: Session = Depends(get_session)) -> Response:
    tables = {"jobs": Job, "status_history": StatusHistory, "agent_runs": AgentRun, "applications": Application,
              "application_fields": ApplicationField, "answers": Answer, "settings": Setting}
    payload = {name: [row.model_dump() for row in session.exec(select(model))] for name, model in tables.items()}
    body = json.dumps({"exported_at": datetime.now().isoformat(timespec="seconds"), **payload},
                      default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v), ensure_ascii=False, indent=1)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="jobpilot_{today_ist().isoformat()}.json"'})


@router.post("/examples/seed")
def seed_examples(session: Session = Depends(get_session)) -> dict[str, int]:
    added = seed(session)
    session.commit()
    return {"added_jobs": added}


@router.delete("/examples")
def purge_examples(session: Session = Depends(get_session)) -> dict[str, int]:
    counts = purge(session)
    session.commit()
    return counts
