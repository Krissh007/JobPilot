"""
Runs the legacy discovery pipeline from E:\\job_search_agent unchanged.

Executed by the LEGACY Python with cwd = the legacy project, never imported
by JobPilot. It only switches two stages off, in memory for this run:
- tailoring (JobPilot's docs agent does that, so Gemini quota isn't spent twice)
- prepare_after_search (it opens apply tabs; JobPilot's apply agent owns that)

    python legacy_shim.py --legacy-dir E:\\job_search_agent [--keywords ...] [--sources ...]

Prints one final line: JOBPILOT_RESULT {json stats}
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-dir", required=True)
    parser.add_argument("--keywords", nargs="+")
    parser.add_argument("--sources", nargs="+")
    args = parser.parse_args()

    root = Path(args.legacy_dir).resolve()
    sys.path.insert(0, str(root))

    from src.config import Settings, validate_model
    from src.pipelines import discovery_pipeline

    context = discovery_pipeline.load_context(root)
    data = context.settings.model_dump()
    data["tailoring"]["enabled"] = False
    data["application"]["prepare_after_search"] = False
    if args.keywords:
        data["search"]["keywords"] = args.keywords
    if args.sources:
        data["sources"] = args.sources
    context.settings = validate_model(Settings, data, "JobPilot overrides")

    discovery_pipeline.configure_logging(context)
    print(f"Legacy pipeline | sources={context.settings.sources} keywords={context.settings.search.keywords}", flush=True)
    stats = discovery_pipeline.run(context)
    print("JOBPILOT_RESULT " + json.dumps(asdict(stats)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
