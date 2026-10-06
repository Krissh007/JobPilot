"""
Search agent: run the legacy agent's discovery + matching and save the matches
straight into jobpilot.db (the only database).

    python -m jobpilot.agents.search                      # browser search + save
    python -m jobpilot.agents.search --keywords "AI Business Analyst" --sources naukri
    python -m jobpilot.agents.search --then-score         # scheduled task: search, then score
    python -m jobpilot.agents.search --import-legacy-db   # one-off: copy jobs from the old legacy jobs.db archive

agent_runs.found = new jobs, processed = matched jobs received.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from jobpilot.agents._run import RunRecorder, agent_run
from jobpilot.config import DATA_DIR, get_config
from jobpilot.db.models import AgentName, Job
from jobpilot.db.repo import get_setting
from jobpilot.db.session import get_engine
from jobpilot.legacy.importer import import_legacy, import_rows

SHIM = Path(__file__).resolve().parents[1] / "legacy" / "legacy_shim.py"
JOB_PREFIX = "JOBPILOT_JOB "
RESULT_PREFIX = "JOBPILOT_RESULT "


def run_legacy_search(rec: RunRecorder, legacy_dir: Path, python: str, known_urls: list[str],
                      keywords: list[str] | None, sources: list[str] | None) -> tuple[dict | None, list[dict[str, Any]]]:
    """Run the shim; returns (legacy stats, matched job dicts)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    known_file = DATA_DIR / "known_urls.json"
    known_file.write_text(json.dumps(known_urls), encoding="utf-8")

    command = [python, str(SHIM), "--legacy-dir", str(legacy_dir), "--known-urls", str(known_file),
               "--debug-dir", str(DATA_DIR / "debug")]
    if keywords:
        command += ["--keywords", *keywords]
    if sources:
        command += ["--sources", *sources]
    rec.log(f"Running legacy discovery ({len(known_urls)} known URLs skipped)")

    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen(command, cwd=legacy_dir, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    stats, jobs = None, []
    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.rstrip()
        if line.startswith(JOB_PREFIX):
            jobs.append(json.loads(line[len(JOB_PREFIX):]))
        elif line.startswith(RESULT_PREFIX):
            stats = json.loads(line[len(RESULT_PREFIX):])
        elif line:
            rec.log("[legacy] " + line)
    code = process.wait()
    if code != 0:
        rec.log(f"Legacy discovery exited with code {code}; saving the {len(jobs)} job(s) it matched before that")
    return stats, jobs


def run(import_legacy_db: bool = False, keywords: list[str] | None = None,
        sources: list[str] | None = None, db_url: str | None = None, run_id: int | None = None) -> None:
    config = get_config()
    legacy_dir = Path(config.legacy_agent_dir)
    with agent_run(AgentName.search, db_url, run_id) as rec:
        with Session(get_engine(db_url)) as session:
            if import_legacy_db:
                result = import_legacy(session, legacy_dir, log=rec.log)
            else:
                known = list(session.exec(select(Job.url)))
                keywords = keywords or get_setting(session, "search_queries")  # Settings screen is the source of truth
                rec.log(f"Keywords: {', '.join(keywords)}")
                stats, jobs = run_legacy_search(rec, legacy_dir, config.legacy_python, known, keywords, sources)
                if stats:
                    rec.log(f"Legacy stats: discovered {stats['discovered']}, stale {stats['stale']}, "
                            f"rejected by gates {stats['rejected']}, matched {stats['matched']}")
                result = import_rows(session, jobs, source="legacy discovery")
            session.commit()
        rec.found, rec.processed = result.created, result.seen
        rec.log(result.summary())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--import-legacy-db", action="store_true",
                        help="Skip the browser search; copy jobs from the old legacy data/jobs.db (read-only)")
    parser.add_argument("--keywords", nargs="+", help="Override the search queries from Settings for this run")
    parser.add_argument("--sources", nargs="+", help="Override the legacy sources for this run (naukri, linkedin)")
    parser.add_argument("--then-score", action="store_true", help="Run the score agent afterwards (even if search failed)")
    parser.add_argument("--run-id", type=int, help=argparse.SUPPRESS)  # set by the dashboard API
    args = parser.parse_args(argv)
    failed = False
    try:
        run(args.import_legacy_db, args.keywords, args.sources, run_id=args.run_id)
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
