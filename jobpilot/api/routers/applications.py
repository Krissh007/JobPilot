"""
Review queue. The apply agent (Phase 7) fills a form, screenshots it and
waits in its browser tab; these endpoints record your decision, which the
agent polls for. Nothing here ever touches a browser itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from jobpilot.api.deps import get_or_404, get_session
from jobpilot.db.models import AgentName, AgentRun, Application, ApplicationField, ApplicationState, Job, RunStatus, utcnow

router = APIRouter(tags=["applications"])


def serialize(app: Application, session: Session) -> dict[str, Any]:
    job = session.get(Job, app.job_id)
    fields = session.exec(select(ApplicationField).where(ApplicationField.application_id == app.id)
                          .order_by(ApplicationField.step, ApplicationField.id)).all()
    return {
        **app.model_dump(),
        "has_screenshot": bool(app.screenshot_path) and Path(app.screenshot_path).is_file(),
        "job": {"id": job.id, "title": job.title, "company": job.company, "score": job.score,
                "source": job.source, "url": job.url} if job else None,
        "fields": [f.model_dump(exclude={"application_id"}) for f in fields],
        "needs_review": sum(f.needs_review for f in fields),
    }


@router.get("/applications")
def list_applications(state: Optional[list[ApplicationState]] = Query(None),
                      session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    query = select(Application).order_by(Application.created_at.desc())
    if state:
        query = query.where(Application.state.in_(state))
    return [serialize(app, session) for app in session.exec(query)]


@router.get("/applications/{app_id}")
def get_application(app_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    return serialize(get_or_404(session, Application, app_id), session)


@router.get("/applications/{app_id}/screenshot")
def screenshot(app_id: int, session: Session = Depends(get_session)):
    app = get_or_404(session, Application, app_id)
    path = Path(app.screenshot_path) if app.screenshot_path else None
    if path is None or not path.is_file():
        raise HTTPException(404, "No screenshot for this application")
    return FileResponse(path, media_type="image/png")


def decide(app_id: int, to: ApplicationState, session: Session) -> dict[str, Any]:
    app = get_or_404(session, Application, app_id)
    if app.state != ApplicationState.pending_review:
        raise HTTPException(409, f"Application {app_id} is '{app.state.value}', not waiting for review")
    if to == ApplicationState.approved and app.reason == "incomplete":
        raise HTTPException(409, "This form isn't on its Submit page yet. Finish the missing steps in the agent's browser "
                                 "window; Approve unlocks once it reaches Submit, or submit it there yourself.")
    if to == ApplicationState.approved and not app.is_example:
        waiting = session.exec(select(AgentRun).where(AgentRun.agent == AgentName.apply, AgentRun.status == RunStatus.running)).first()
        if waiting is None:
            # Approval only means something while the agent holds the filled form open in its tab.
            raise HTTPException(409, "The apply agent is no longer holding this form open. Use 'Queue for apply' on the job to fill it again.")
    app.state, app.decided_at = to, utcnow()
    session.add(app)
    session.commit()
    return serialize(app, session)


@router.post("/applications/{app_id}/approve")
def approve(app_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Approve submit. The waiting apply agent clicks Submit in its own browser tab."""
    return decide(app_id, ApplicationState.approved, session)


@router.post("/applications/{app_id}/skip")
def skip(app_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    return decide(app_id, ApplicationState.skipped, session)
