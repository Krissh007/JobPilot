"""
Runs that never recorded an outcome. An agent that is killed hard (power off, Task Scheduler stopping it, a closed
terminal) never reaches the code that writes "error", and nothing else looks at its row afterwards, so it showed
"running" for ever (discovery runs 183 and 184, 28 Oct 2026). `fail_stuck_runs` is called when the API starts: a run
still "running" after STUCK_AFTER is marked as an error with a note. Demo rows are left alone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from jobpilot.db.models import AgentRun, RunStatus, utcnow
from jobpilot.db.session import get_engine

log = logging.getLogger("jobpilot.stuck_runs")

STUCK_AFTER = timedelta(hours=24)   # a scheduled pass is stopped after 2 h, so nothing legitimate runs this long
NOTE = "Marked as failed: it was still 'running' after {hours} hours; the process most likely died without recording an outcome."


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def fail_stuck_runs(db_url: str | None = None, *, older_than: timedelta = STUCK_AFTER, now: datetime | None = None) -> list[int]:
    """Mark runs stuck "running" for longer than `older_than` as errors. Returns the ids it changed."""
    now = now or utcnow()
    changed: list[int] = []
    with Session(get_engine(db_url)) as session:
        running = session.exec(select(AgentRun).where(AgentRun.status == RunStatus.running, AgentRun.is_example.is_(False))).all()
        for run in running:
            if now - _aware(run.started_at) <= older_than:
                continue
            note = NOTE.format(hours=int(older_than.total_seconds() // 3600))
            run.status, run.finished_at, run.error = RunStatus.error, now, note
            run.log = f"{run.log}\n{note}" if run.log else note
            session.add(run)
            changed.append(run.id)
        session.commit()
    if changed:
        log.warning("marked %d stuck run(s) as failed: %s", len(changed), ", ".join(map(str, changed)))
    return changed
