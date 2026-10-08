"""
Windows Task Scheduler status for the dashboard, and register / remove buttons.
The tasks themselves are created by scripts/register_tasks.ps1 (same script the README tells you to run).
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from jobpilot.api.deps import get_session
from jobpilot.config import ROOT
from jobpilot.scheduled import LOG, TASKS, config

router = APIRouter(tags=["schedule"])
SCRIPT = ROOT / "scripts" / "register_tasks.ps1"
_cache: dict[str, Any] = {"at": 0.0, "tasks": None}

STATUS_PS = r"""
$names = '__NAMES__' | ConvertFrom-Json
$out = foreach ($n in $names) {
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if ($t) {
    $i = $t | Get-ScheduledTaskInfo
    [pscustomobject]@{ name = $n; registered = $true; state = "$($t.State)"
      next_run = if ($i.NextRunTime) { $i.NextRunTime.ToString('o') } else { $null }
      last_run = if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { $i.LastRunTime.ToString('o') } else { $null }
      last_result = $i.LastTaskResult }
  } else { [pscustomobject]@{ name = $n; registered = $false } }
}
ConvertTo-Json -InputObject @($out) -Compress
"""


def powershell(script: str, timeout: float = 60) -> str:
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                            capture_output=True, text=True, timeout=timeout, cwd=ROOT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip().splitlines()[-1] if (result.stderr or result.stdout) else "PowerShell failed")
    return result.stdout


def task_status(max_age: float = 20) -> list[dict[str, Any]]:
    if _cache["tasks"] is None or time.monotonic() - _cache["at"] > max_age:
        names = json.dumps(list(TASKS.values()))
        _cache["tasks"] = json.loads(powershell(STATUS_PS.replace("__NAMES__", names)) or "[]")
        _cache["at"] = time.monotonic()
    return _cache["tasks"]


def log_tail(lines: int = 12) -> list[str]:
    try:
        return LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except FileNotFoundError:
        return []


def result_text(code: int | None) -> str:
    return {None: "", 0: "ok", 1: "finished with a failed step", 267009: "running", 267011: "not run yet",
            267014: "stopped by you", 2147750687: "already running"}.get(code, f"error 0x{(code or 0) & 0xFFFFFFFF:08X}")


@router.get("/schedule")
def get_schedule(session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        tasks = [{**t, "last_result_text": result_text(t.get("last_result")) if t.get("registered") else ""}
                 for t in task_status()]
        error = ""
    except Exception as exc:  # Task Scheduler unavailable: the dashboard still loads
        tasks, error = [], str(exc)[:300]
    return {"tasks": tasks, "error": error, "config": config(session), "log_tail": log_tail(),
            "command": f'powershell -ExecutionPolicy Bypass -File "{SCRIPT}"', "checked_at": datetime.now().isoformat()}


def run_script(session: Session, *flags: str) -> dict[str, Any]:
    try:
        output = powershell(f'& "{SCRIPT}" {" ".join(flags)}', timeout=120)
    except Exception as exc:
        raise HTTPException(500, f"Task Scheduler: {exc}") from exc
    _cache["tasks"] = None
    return {"output": output.strip().splitlines(), **get_schedule(session)}


@router.post("/schedule/register")
def register(session: Session = Depends(get_session)) -> dict[str, Any]:
    """Create or update both tasks from the current Settings (your click is the go-ahead)."""
    return run_script(session)


@router.post("/schedule/unregister")
def unregister(session: Session = Depends(get_session)) -> dict[str, Any]:
    return run_script(session, "-Unregister")
