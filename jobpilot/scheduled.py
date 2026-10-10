"""
The scheduled pipeline Windows Task Scheduler runs (Phase 8): search -> score -> docs.

    python -m jobpilot.scheduled                  # one pass now, any time
    python -m jobpilot.scheduled --log            # what the task runs: logs to a file, skips late runs
    python -m jobpilot.scheduled --catch-up       # on sign-in/unlock/wake: a pass only if one is overdue
    python -m jobpilot.scheduled --print-config   # JSON used by scripts/register_tasks.ps1

Each agent records its own row in agent_runs, so the dashboard shows scheduled runs
like any other. Apply is never scheduled: it opens a browser and waits for you.
A lock file stops two passes overlapping (a slow search can outlast the interval).

Windows does not reliably start a missed run after the PC wakes from sleep or modern standby
(9-10 Oct 2026: 7 runs missed in a row). The catch-up task fires on sign-in, unlock and wake and
runs a pass only when the last search is older than one interval and it is inside the window.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from sqlmodel import Session, select

from jobpilot.config import DATA_DIR
from jobpilot.db.repo import get_setting
from jobpilot.db.session import session_scope

LOCK = DATA_DIR / "scheduled.lock"
LOG = DATA_DIR / "logs" / "scheduled.log"
TASKS = {"pipeline": "JobPilot Search-Score-Docs", "weekly": "JobPilot Weekly-Answer-Review",
         "catchup": "JobPilot Catch-up"}
# A catch-up pass runs when the last search started at least this long before one interval ends,
# so a wake shortly before a regular run doesn't start a duplicate pass.
CATCH_UP_SLACK = timedelta(minutes=30)


def config(session: Session | None = None) -> dict:
    """Schedule settings in the shape the register script needs."""
    if session is None:
        with session_scope() as own:
            return config(own)
    schedule = get_setting(session, "schedule")
    weekly = get_setting(session, "weekly_review")
    start_h, start_m = (int(x) for x in schedule["start"].split(":"))
    end_h, end_m = (int(x) for x in schedule["end"].split(":"))
    window = (end_h * 60 + end_m) - (start_h * 60 + start_m)
    return {"tasks": TASKS, "start": schedule["start"], "every_hours": int(schedule["every_hours"]),
            "window_minutes": max(window, 0), "weekly_day": weekly["day"], "weekly_time": weekly["time"]}


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock(lock: Path = LOCK) -> bool:
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        pid = int(lock.read_text().strip())
        if pid != os.getpid() and _pid_alive(pid):
            return False
    except (FileNotFoundError, ValueError):
        pass
    lock.write_text(str(os.getpid()))
    return True


def default_steps() -> list[tuple[str, Callable[[], None]]]:
    from jobpilot.agents import docs, score, search

    return [("search", search.run), ("score", score.run), ("docs", docs.run)]


def run_pipeline(steps: list[tuple[str, Callable[[], None]]] | None = None, lock: Path = LOCK) -> int:
    """Run every step even if an earlier one failed (score can still work on older jobs). Returns an exit code."""
    if not acquire_lock(lock):
        print(f"{datetime.now():%Y-%m-%d %H:%M} another scheduled pass is still running; skipped", flush=True)
        return 0
    failed = []
    try:
        print(f"{datetime.now():%Y-%m-%d %H:%M} scheduled pass started", flush=True)
        for name, step in steps or default_steps():
            try:
                step()
            except Exception as error:  # already recorded in agent_runs
                failed.append(name)
                print(f"{name} failed: {error}", flush=True)
        print(f"{datetime.now():%Y-%m-%d %H:%M} scheduled pass finished"
              + (f" with failures: {', '.join(failed)}" if failed else ""), flush=True)
    finally:
        lock.unlink(missing_ok=True)
    return 1 if failed else 0


GRACE_MINUTES = 30  # a run Windows starts late (PC was asleep) still counts if it's this close to the window


def within_window(now: datetime, start: str, window_minutes: int, grace: int = GRACE_MINUTES) -> bool:
    """True if now (local IST time) is between start and start+window (+grace). Missed runs after that are skipped."""
    hour, minute = (int(x) for x in start.split(":"))
    minutes = now.hour * 60 + now.minute
    return hour * 60 + minute <= minutes <= hour * 60 + minute + window_minutes + grace


def last_search_started(session: Session | None = None) -> datetime | None:
    """Start time (UTC) of the newest real search run, whatever its outcome."""
    if session is None:
        with session_scope() as own:
            return last_search_started(own)
    from jobpilot.db.models import AgentName, AgentRun

    started = session.exec(
        select(AgentRun.started_at)
        .where(AgentRun.agent == AgentName.search, AgentRun.is_example.is_(False))
        .order_by(AgentRun.started_at.desc())
    ).first()
    if started is not None and started.tzinfo is None:  # SQLite returns naive UTC
        started = started.replace(tzinfo=timezone.utc)
    return started


def catch_up_due(now: datetime, cfg: dict, last_started: datetime | None) -> bool:
    """True when a missed pass should run now: inside the window and the last search is overdue.
    now is local time with tzinfo; last_started is the UTC start of the newest search run."""
    if not within_window(now, cfg["start"], cfg["window_minutes"]):
        return False
    if last_started is None:
        return True
    return now - last_started >= timedelta(hours=cfg["every_hours"]) - CATCH_UP_SLACK


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print-config", action="store_true", help="Print the schedule settings as JSON and exit")
    parser.add_argument("--log", action="store_true", help=f"Append output to {LOG} (used by the scheduled task)")
    parser.add_argument("--catch-up", action="store_true",
                        help="Run a pass only if one is overdue (used by the sign-in/unlock/wake task)")
    args = parser.parse_args(argv)
    if args.print_config:
        print(json.dumps(config()))
        return
    if args.catch_up:
        cfg = config()
        now = datetime.now().astimezone()
        if not catch_up_due(now, cfg, last_search_started()):
            sys.exit(0)  # quiet: this fires on every unlock and wake
        args.log = True
    if args.log or sys.stdout is None:  # pythonw.exe has no console
        LOG.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)
    if args.catch_up:
        print(f"{datetime.now():%Y-%m-%d %H:%M} catch-up: the last search is overdue (PC was asleep or off)",
              flush=True)
    elif args.log:  # the scheduled task: Windows may start a missed run hours late (StartWhenAvailable)
        cfg = config()
        if not within_window(datetime.now(), cfg["start"], cfg["window_minutes"]):
            print(f"{datetime.now():%Y-%m-%d %H:%M} outside the schedule window ({cfg['start']} + "
                  f"{cfg['window_minutes'] // 60} h); skipped this late run", flush=True)
            sys.exit(0)
    sys.exit(run_pipeline())


if __name__ == "__main__":
    main()
