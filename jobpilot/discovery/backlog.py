"""
Enrichment backlog: `found`, unscored jobs saved as bare cards (no usable description) are
enriched on later runs, freshest first, within `backlog_budget`. Ones past the freshness
window are expired instead (found -> rejected, note prefixed "Expired:").

Posting date = jobs.posted_on, else the day the job was found (a lower bound on its age).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from sqlalchemy import func
from sqlmodel import Session, select

from jobpilot.scoring.common import MIN_JD_CHARS
from jobpilot.db.models import Actor, Job, JobStatus
from jobpilot.db.repo import set_status
from jobpilot.discovery.cards import Card
from jobpilot.discovery.config import DiscoveryConfig
from jobpilot.discovery.sources.base import JobSource, QueryBlocked
from jobpilot.experience import experience_from_jd

EXPIRED_PREFIX = "Expired:"
ENRICH_FAILED_PREFIX = "Enrich:"  # job's page gave no description; not retried (the expiry window bounds it anyway)


@dataclass
class BacklogReport:
    expired: int = 0
    enriched: int = 0
    failed: int = 0
    waiting: int = 0   # still unenriched after this run

    def summary(self) -> str:
        return (f"Backlog: expired {self.expired}, enriched {self.enriched}, no description {self.failed}, "
                f"{self.waiting} still waiting")


def effective_posted(job: Job) -> date:
    return job.posted_on or job.found_at.date()


def _card(job: Job) -> Card:
    return Card(title=job.title, company=job.company, url=job.url, source=job.source, location=job.location,
                exp_min=job.exp_min, exp_max=job.exp_max)


def run_backlog(session: Session, config: DiscoveryConfig, sources: dict[str, JobSource],
                today: date | None = None, log: Callable[[str], None] = print) -> BacklogReport:
    today = today or date.today()
    report = BacklogReport()
    pending = [j for j in session.exec(select(Job).where(
        Job.status == JobStatus.found, Job.score.is_(None), Job.is_example.is_(False),
        func.length(Job.jd_text) < MIN_JD_CHARS)) if ENRICH_FAILED_PREFIX not in j.notes]

    live: list[Job] = []
    for job in pending:
        age = (today - effective_posted(job)).days
        if age > config.max_posted_days:
            job.notes = f"{job.notes}\n{EXPIRED_PREFIX} posted {effective_posted(job)} ({age} days, window {config.max_posted_days})".strip()
            set_status(session, job, JobStatus.rejected, actor=Actor.agent)
            report.expired += 1
        else:
            live.append(job)
    session.commit()

    live.sort(key=lambda j: (effective_posted(j), j.found_at), reverse=True)  # freshest first
    blocked: set[str] = set()
    budget = config.backlog_budget
    for job in live:
        source = sources.get(job.source)
        if budget <= 0 or source is None or job.source in blocked:
            continue
        budget -= 1
        card = _card(job)
        try:
            source.enrich(card)
        except QueryBlocked as error:
            blocked.add(job.source)
            log(f"Backlog: {job.source} blocked ({error}); skipping its remaining jobs this run")
            continue
        except Exception as error:
            log(f"Backlog: could not enrich #{job.id} {job.title}: {error}")
            continue
        if card.description:
            job.jd_text = card.description
            if job.exp_min is None and job.exp_max is None:
                job.exp_min, job.exp_max = experience_from_jd(card.description)
            report.enriched += 1
        else:
            job.notes = f"{job.notes}\n{ENRICH_FAILED_PREFIX} no description on {today}".strip()
            report.failed += 1
        session.add(job)
        session.commit()
    report.waiting = len(live) - report.enriched - report.failed
    log(report.summary())
    return report
