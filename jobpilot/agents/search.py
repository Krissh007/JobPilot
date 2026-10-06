"""
Search agent: run the legacy job search, then import what it found.

    python -m jobpilot.agents.search                      # full legacy search + import
    python -m jobpilot.agents.search --import-only        # just import legacy jobs.db
    python -m jobpilot.agents.search --keywords "AI Business Analyst" --sources naukri
    python -m jobpilot.agents.search --then-score         # scheduled task: search, then score

agent_runs.found = new jobs, processed = legacy rows seen.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from sqlmodel import Session

from jobpilot.agents._run import RunRecorder, agent_run
from jobpilot.config import get_config
from jobpilot.db.models import AgentName
from jobpilot.db.session import get_engine
from jobpilot.legacy.importer import import_legacy

SHIM = Path(__file__).resolve().parents[1] / "legacy" / "legacy_shim.py"
RESULT_PREFIX = "JOBPILOT_RESULT "


def run_legacy_search(rec: RunRecorder, legacy_dir: Path, python: str,
                      keywords: list[str] | None, sources: list[str] | None) -> dict | None:
    command = [python, str(SHIM), "--legacy-dir", str(legacy_dir)]
    if keywords:
        command += ["--keywords", *keywords]
    if sources:
        command += ["--sources", *sources]
    rec.log("Running legacy search: " + " ".join(f'"{c}"' if " " in c else c for c in command))

    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen(command, cwd=legacy_dir, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    result = None
    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.rstrip()
        if line.startswith(RESULT_PREFIX):
            result = json.loads(line[len(RESULT_PREFIX):])
        elif line:
            rec.log("[legacy] " + line)
    code = process.wait()
    if code != 0:
        rec.log(f"Legacy search exited with code {code}; importing whatever it saved")
    return result


def run(import_only: bool = False, keywords: list[str] | None = None,
        sources: list[str] | None = None, db_url: str | None = None, run_id: int | None = None) -> None:
    config = get_config()
    legacy_dir = Path(config.legacy_agent_dir)
    with agent_run(AgentName.search, db_url, run_id) as rec:
        if not import_only:
            stats = run_legacy_search(rec, legacy_dir, config.legacy_python, keywords, sources)
            if stats:
                rec.log(f"Legacy stats: discovered {stats['discovered']}, matched {stats['matched']}, "
                        f"new {stats['new']}, rejected {stats['rejected']}, stale {stats['stale']}")
        with Session(get_engine(db_url)) as session:
            result = import_legacy(session, legacy_dir, log=rec.log)
            session.commit()
        rec.found, rec.processed = result.created, result.seen
        rec.log(result.summary())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--import-only", action="store_true", help="Skip the browser search; import legacy jobs.db only")
    parser.add_argument("--keywords", nargs="+", help="Override the legacy search keywords for this run")
    parser.add_argument("--sources", nargs="+", help="Override the legacy sources for this run (naukri, linkedin)")
    parser.add_argument("--then-score", action="store_true", help="Run the score agent afterwards (even if search failed)")
    parser.add_argument("--run-id", type=int, help=argparse.SUPPRESS)  # set by the dashboard API
    args = parser.parse_args(argv)
    failed = False
    try:
        run(args.import_only, args.keywords, args.sources, run_id=args.run_id)
    except Exception:
        failed = True  # already recorded in agent_runs and printed
    if args.then_score:
        from jobpilot.agents import score

        try:
            score.run()
        except Exception:
            failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
