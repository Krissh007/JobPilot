"""
Runs the legacy agent's discovery and matching stages from E:\\job_search_agent
(unchanged code) and streams the matched jobs to JobPilot on stdout.

Executed by the LEGACY Python with cwd = the legacy project, never imported by
JobPilot. Only two stages run:

    discover_jobs  (browser search on Naukri/LinkedIn, skipping URLs JobPilot already has)
    score_jobs     (normalize, freshness filter, legacy rule match + quality gates)

The legacy save/tailor/prepare/export/sync stages are NOT called, so the legacy
jobs.db, Excel exports and project_sync files are never written. jobpilot.db is
the only database.

    python legacy_shim.py --legacy-dir E:\\job_search_agent --known-urls known.json
                          --debug-dir E:\\JobPilot\\data\\debug [--keywords ...] [--sources ...]

stdout protocol: one "JOBPILOT_JOB {json}" line per matched job, then
"JOBPILOT_RESULT {json stats}". Everything else is log output.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-dir", required=True)
    parser.add_argument("--known-urls", required=True, help="JSON list of job URLs JobPilot already has")
    parser.add_argument("--debug-dir", required=True, help="Where sources may dump pages when a search returns 0 jobs")
    parser.add_argument("--keywords", nargs="+")
    parser.add_argument("--sources", nargs="+")
    args = parser.parse_args()

    root = Path(args.legacy_dir).resolve()
    sys.path.insert(0, str(root))

    from src.config import Settings, validate_model
    from src.pipelines.discovery_pipeline import RunStats, discover_jobs, load_context, score_jobs

    context = load_context(root)
    data = context.settings.model_dump()
    data["project"]["debug_directory"] = str(Path(args.debug_dir).resolve())  # keep the legacy folder untouched
    if args.keywords:
        data["search"]["keywords"] = args.keywords
    if args.sources:
        data["sources"] = args.sources
    context.settings = validate_model(Settings, data, "JobPilot overrides")

    # Log to stdout only (the legacy configure_logging would also write into the legacy data/logs).
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s", stream=sys.stdout, force=True)
    known = set(json.loads(Path(args.known_urls).read_text(encoding="utf-8")))
    print(f"Legacy discovery | sources={context.settings.sources} keywords={context.settings.search.keywords} "
          f"| skipping {len(known)} known URLs", flush=True)

    stats = RunStats()
    raw = discover_jobs(context, known, stats)
    matched = score_jobs(raw, context, stats)
    for job in matched:
        print("JOBPILOT_JOB " + json.dumps(job.to_dict(), default=str, ensure_ascii=False), flush=True)
    print("JOBPILOT_RESULT " + json.dumps(asdict(stats)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
