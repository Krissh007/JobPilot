"""
agent_runs bookkeeping shared by every agent. The run row is committed as
soon as it starts and the log is flushed every couple of seconds, so the
dashboard can show a live "running" state and tail the log.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from sqlmodel import Session

from jobpilot.db.models import AgentName, AgentRun, RunStatus, utcnow
from jobpilot.db.session import get_engine

MAX_LOG_CHARS = 200_000  # keep the newest part of very long logs


class RunRecorder:
    def __init__(self, run_id: int, db_url: str | None, flush_every: float = 2.0) -> None:
        self.run_id = run_id
        self.db_url = db_url
        self.flush_every = flush_every
        self.lines: list[str] = []
        self.found = 0
        self.processed = 0
        self._last_flush = 0.0

    def log(self, message: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {message}"
        print(line, flush=True)
        self.lines.append(line)
        if time.monotonic() - self._last_flush >= self.flush_every:
            self.flush()

    def flush(self, **final) -> None:
        with Session(get_engine(self.db_url)) as session:
            run = session.get(AgentRun, self.run_id)
            run.log = "\n".join(self.lines)[-MAX_LOG_CHARS:]
            run.found, run.processed = self.found, self.processed
            for key, value in final.items():
                setattr(run, key, value)
            session.add(run)
            session.commit()
        self._last_flush = time.monotonic()


RUN_CALLER_ENV = "JOBPILOT_RUN_CALLER"                     # set by the API for runs it starts ("dashboard" or "agent")
RUN_REQUESTED_ENV = "JOBPILOT_RUN_AUTO_SUBMIT_REQUESTED"   # "1" / "0": what the caller asked for (pipeline runs)
RUN_SETTING_ENV = "JOBPILOT_RUN_AUTO_SUBMIT_SETTING"       # "1" / "0": the Settings switch when the run was started


def _flag(name: str) -> bool | None:
    value = os.environ.get(name)
    return None if value not in ("0", "1") else value == "1"


def record_origin(run: AgentRun) -> None:
    """Fill in who started this run and, for apply, what auto-submit was asked for, unless the API already did."""
    if run.caller is None:
        run.caller = os.environ.get(RUN_CALLER_ENV) or None
    if run.agent == AgentName.apply:
        if run.auto_submit_requested is None:
            run.auto_submit_requested = _flag(RUN_REQUESTED_ENV)
        if run.auto_submit_setting is None:
            run.auto_submit_setting = _flag(RUN_SETTING_ENV)


@contextmanager
def agent_run(agent: AgentName, db_url: str | None = None, run_id: int | None = None) -> Iterator[RunRecorder]:
    """run_id: a row the API created already (POST /api/runs/{agent}), so it can stream it at once."""
    with Session(get_engine(db_url)) as session:
        run = session.get(AgentRun, run_id) if run_id is not None else None
        if run is None or run.agent != agent:
            run = AgentRun(agent=agent)
        record_origin(run)
        run.status, run.started_at = RunStatus.running, utcnow()
        session.add(run)
        session.commit()
        run_id, existing = run.id, run.log

    recorder = RunRecorder(run_id, db_url)
    # Append to what the API already wrote, so a client tailing the log never loses a line.
    recorder.lines = existing.splitlines() if existing else []
    recorder.log(f"{agent.value} run #{run_id} started")
    caller = os.environ.get(RUN_CALLER_ENV)
    if agent == AgentName.apply and caller and "started by" not in (existing or ""):   # pipeline rows are made here, so the API's own line isn't there
        recorder.log(f"started by {caller}")
    try:
        yield recorder
    except BaseException as error:  # includes Ctrl+C, so the row never stays "running"
        message = f"{type(error).__name__}: {error}".strip(": ")
        recorder.log(f"FAILED {message}")
        recorder.flush(status=RunStatus.error, error=message[:1000], finished_at=utcnow())
        raise
    recorder.log(f"finished: found {recorder.found}, processed {recorder.processed}")
    recorder.flush(status=RunStatus.ok, finished_at=utcnow())
