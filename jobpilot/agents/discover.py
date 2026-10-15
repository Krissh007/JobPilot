"""
Agent 1: Discovery (the redesign). 

    python -m jobpilot.agents.discover                              # settings from the DB ('discovery' key)
    python -m jobpilot.agents.discover --sources linkedin --keywords "Business Analyst" --enrich-per-query 5
    python -m jobpilot.agents.discover --dry-run                    # print the planned queries, open no browser

Writes jobs at status `found` (Scoring picks them up from there). After the queries it works the enrichment
backlog: unenriched `found` jobs get their description freshest-first within `backlog_budget`; those past
the freshness window are expired (found -> rejected, note "Expired: ...").
agent_runs.found = new jobs saved, processed = cards fetched. Logged under the `discovery` agent name.
"""

from __future__ import annotations

import argparse
import sys

from sqlmodel import Session

from jobpilot.agents._run import agent_run
from jobpilot.db.models import AgentName
from jobpilot.db.session import get_engine
from jobpilot.discovery.backlog import run_backlog
from jobpilot.discovery.config import DiscoveryConfig
from jobpilot.discovery.pipeline import build_queries, run_discovery
from jobpilot.discovery.sources.linkedin import LinkedInSource
from jobpilot.discovery.sources.naukri import NaukriSource


def run(overrides: dict | None = None, dry_run: bool = False, db_url: str | None = None,
        run_id: int | None = None, sources: dict | None = None) -> None:
    """`sources` lets tests inject fakes; otherwise a browser is launched for the configured portals."""
    with agent_run(AgentName.discovery, db_url, run_id) as rec:
        with Session(get_engine(db_url)) as session:
            config = DiscoveryConfig(**{**DiscoveryConfig.from_settings(session).model_dump(), **(overrides or {})})
            queries = build_queries(config)
            rec.log(f"Discovery: {len(queries)} queries, last {config.max_posted_days} days, enrich budget "
                    f"{config.enrich_per_query}/query, variants {'on' if config.use_variants else 'off'}")
            if dry_run:
                for q in queries:
                    rec.log(f"  {q.source} | {q.keyword} | {q.location}")
                return
            if sources is not None:
                report = _discover_and_backlog(session, config, sources, rec.log)
            else:
                report = _run_with_browser(session, config, rec.log)
        rec.found, rec.processed = report.total("saved"), report.total("fetched")
        rec.log(report.summary())


def _discover_and_backlog(session, config: DiscoveryConfig, sources: dict, log):
    """New cards first, then the leftover unenriched `found` jobs (freshest first; stale ones expire)."""
    report = run_discovery(session, config, sources, log=log)
    run_backlog(session, config, sources, log=log)
    return report


def _run_with_browser(session, config: DiscoveryConfig, log):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=False)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="en-IN", timezone_id="Asia/Kolkata")
        context.set_default_timeout(30_000)
        page = context.new_page()
        naukri = NaukriSource(page, config.naukri_sort_param)
        sources = {
            "naukri": naukri,
            "linkedin": LinkedInSource(page.request, config.linkedin_easy_apply_only,
                                       sleep_ms=lambda ms: page.wait_for_timeout(ms)),
        }
        try:
            return _discover_and_backlog(session, config, {k: v for k, v in sources.items() if k in config.sources}, log)
        finally:
            naukri.close()
            context.close()
            browser.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keywords", nargs="+")
    parser.add_argument("--sources", nargs="+")
    parser.add_argument("--locations", nargs="+")
    parser.add_argument("--max-posted-days", type=int)
    parser.add_argument("--max-cards-per-query", type=int)
    parser.add_argument("--max-pages-per-query", type=int)
    parser.add_argument("--enrich-per-query", type=int)
    parser.add_argument("--no-variants", action="store_true", help="Search only the configured keywords")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-id", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    overrides = {k: v for k, v in {
        "keywords": args.keywords, "sources": args.sources, "locations": args.locations,
        "max_posted_days": args.max_posted_days, "max_cards_per_query": args.max_cards_per_query,
        "max_pages_per_query": args.max_pages_per_query, "enrich_per_query": args.enrich_per_query,
        "use_variants": False if args.no_variants else None,
    }.items() if v is not None}
    try:
        run(overrides, args.dry_run, run_id=args.run_id)
    except Exception:
        sys.exit(1)  # already recorded in agent_runs


if __name__ == "__main__":
    main()
