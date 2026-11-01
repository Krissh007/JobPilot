"""
Agent 1 pipeline, per query:

    fetch cards -> freshness (card level) -> dedupe (card level) -> pre-score rank
    -> enrich only the top `enrich_per_query` -> save everything that survived as `found`.

With `strict_titles` (the default) a card is dropped unless its title contains a configured keyword, a registered variant or a related title (whole words).
Only the keywords are searched.
Pre-score ranks; it never drops. A failing query (CAPTCHA, login wall, throttle,
parser error) is recorded and skipped; the run goes on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from sqlmodel import Session

from jobpilot.db.models import Actor, JobStatus
from jobpilot.db.repo import find_duplicate, upsert_job
from jobpilot.discovery.cards import Card, parse_experience, parse_posted
from jobpilot.discovery.config import DiscoveryConfig
from jobpilot.discovery.prescore import prescore
from jobpilot.discovery.sources.base import JobSource, Query, QueryBlocked
from jobpilot.discovery.titles import expand_keywords, matches_registered_title
from jobpilot.experience import experience_from_jd


@dataclass
class QueryResult:
    query: Query
    fetched: int = 0
    off_title: int = 0       # cards whose title contains none of the configured keywords (strict_titles)
    stale: int = 0
    duplicate: int = 0
    enriched: int = 0
    saved: int = 0
    error: str = ""          # set when the query failed; the others still ran
    blocked: str = ""        # QueryBlocked kind, if that was the cause


@dataclass
class DiscoveryReport:
    results: list[QueryResult] = field(default_factory=list)

    def total(self, name: str) -> int:
        return sum(getattr(r, name) for r in self.results)

    @property
    def failed_queries(self) -> int:
        return sum(1 for r in self.results if r.error)

    def summary(self) -> str:
        return (f"{len(self.results)} queries ({self.failed_queries} failed): fetched {self.total('fetched')}, "
                f"off-title {self.total('off_title')}, stale {self.total('stale')}, duplicate {self.total('duplicate')}, "
                f"enriched {self.total('enriched')}, saved {self.total('saved')} as found")


def build_queries(config: DiscoveryConfig) -> list[Query]:
    use_variants = config.use_variants and not config.strict_titles
    keywords = expand_keywords(config.keywords, config.variants if use_variants else [])
    return [Query(source, keyword, location, config.max_posted_days)
            for source in config.sources for keyword in keywords for location in config.locations]


def _prepare(card: Card, query: Query, today: date) -> Card:
    card.query = query.keyword
    if card.posted_on is None:
        card.posted_on = parse_posted(card.posted_text, today)
    if card.exp_min is None and card.experience_text:
        card.exp_min, card.exp_max = parse_experience(card.experience_text)
    return card


def run_discovery(
    session: Session,
    config: DiscoveryConfig,
    sources: dict[str, JobSource],
    today: date | None = None,
    log: Callable[[str], None] = print,
) -> DiscoveryReport:
    today = today or date.today()
    report = DiscoveryReport()
    seen_keys: set[str] = set()
    seen_hashes: dict[str, str] = {}  # hash -> source, so only cross-portal repeats count (as in repo.find_duplicate)

    for query in build_queries(config):
        result = QueryResult(query)
        report.results.append(result)
        label = f"{query.source} | {query.keyword} | {query.location}"
        source = sources.get(query.source)
        try:
            if source is None:
                raise RuntimeError(f"no source configured for {query.source}")
            cards = source.fetch_cards(query, config.max_cards_per_query, config.max_pages_per_query)
        except QueryBlocked as blocked:
            result.error, result.blocked = f"blocked ({blocked})", blocked.kind
            log(f"Query skipped [{label}]: {result.error}")
            continue
        except Exception as error:  # one query must not kill the run
            result.error = f"{type(error).__name__}: {error}"
            log(f"Query failed [{label}]: {result.error}")
            continue

        result.fetched = len(cards)
        survivors: list[Card] = []
        for card in cards:
            _prepare(card, query, today)
            if config.strict_titles and not matches_registered_title(card.title, config.accepted_titles()):
                result.off_title += 1
                continue
            age = card.age_days(today)
            if age is not None and age > config.max_posted_days:   # unknown age is kept
                result.stale += 1
                continue
            if (card.key in seen_keys or seen_hashes.get(card.hash, card.source) != card.source
                    or find_duplicate(session, card.url, card.company, card.title, card.location, card.source)):
                result.duplicate += 1
                continue
            seen_keys.add(card.key)
            seen_hashes.setdefault(card.hash, card.source)
            card.prescore = prescore(card, config, today)
            survivors.append(card)

        # Highest pre-score first; ties go to the newer post, so the budget lands on recent roles.
        survivors.sort(key=lambda c: (-c.prescore, c.age_days(today) if c.age_days(today) is not None else 999))
        for card in survivors[: config.enrich_per_query]:
            try:
                source.enrich(card)
                card.enriched = bool(card.description)
                result.enriched += card.enriched
                if card.exp_min is None and card.exp_max is None:  # card showed no experience: take it from the JD
                    card.exp_min, card.exp_max = experience_from_jd(card.description)
            except QueryBlocked as blocked:
                log(f"Enrichment stopped [{label}]: {blocked}; saving the rest as cards")
                break
            except Exception as error:
                log(f"Could not enrich '{card.title}' ({card.company}): {error}")

        for card in survivors:
            _, created = upsert_job(session, _to_job(card), actor=Actor.agent)
            result.saved += created
        session.commit()
        log(f"[{label}] fetched {result.fetched}, off-title {result.off_title}, stale {result.stale}, dup {result.duplicate}, "
            f"enriched {result.enriched}, saved {result.saved}")

    return report


def _to_job(card: Card) -> dict:
    return {
        "url": card.url, "title": card.title or "(untitled)", "company": card.company or "(unknown)",
        "source": card.source, "location": card.location,
        "exp_min": card.exp_min, "exp_max": card.exp_max, "posted_on": card.posted_on,
        "jd_text": card.description or card.snippet,
        "status": JobStatus.found,
    }
