"""
LinkedIn through the public (logged-out) guest endpoints: 10 cards per request,
then one posting request per card that gets enriched. No account is used.
Anything other than HTTP 200 (after backing off on 429) fails this query only.
"""

from __future__ import annotations

import html
import re
from datetime import date
from typing import Any, Callable
from urllib.parse import urlencode

from jobpilot.discovery.cards import Card, parse_posted
from jobpilot.discovery.sources.base import Query, QueryBlocked

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
VIEW_URL = "https://www.linkedin.com/jobs/view/{job_id}"
PAGE_SIZE = 10
RATE_LIMIT_BACKOFF_MS = (20_000, 60_000)

_TAG = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", fragment)).replace("\xa0", " ").split())


def _inner(chunk: str, tag: str, css_class: str) -> str:
    m = re.search(rf'<{tag}[^>]*class="[^"]*{css_class}[^"]*"[^>]*>(.*?)</{tag}>', chunk, re.S)
    return _text(m.group(1)) if m else ""


def parse_cards(markup: str, today: date | None = None) -> list[Card]:
    today = today or date.today()
    cards: list[Card] = []
    for chunk in re.split(r'(?=<div[^>]*class="[^"]*base-search-card)', markup)[1:]:
        m = re.search(r'data-entity-urn="[^"]*?(\d{6,})"', chunk) or re.search(r"/jobs/view/[^\"?]*?-(\d{6,})", chunk)
        title = _inner(chunk, "h3", "base-search-card__title")
        if not m or not title:
            continue
        stamp = re.search(r'<time[^>]*datetime="([^"]+)"', chunk)
        posted = stamp.group(1) if stamp else ""
        cards.append(Card(
            title=title,
            company=_inner(chunk, "h4", "base-search-card__subtitle"),
            url=VIEW_URL.format(job_id=m.group(1)),
            source="linkedin",
            location=_inner(chunk, "span", "job-search-card__location"),
            posted_text=posted,
            posted_on=parse_posted(posted, today),
            extra={"job_id": m.group(1)},
        ))
    return cards


def parse_description(markup: str) -> str:
    m = re.search(r'<div[^>]*class="[^"]*show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', markup, re.S)
    if not m:
        return ""
    body = re.sub(r"<\s*li\b[^>]*>", "\n- ", m.group(1), flags=re.I)
    body = re.sub(r"<\s*/?\s*(?:br|p|ul|ol|li)\b[^>]*>", "\n", body, flags=re.I)
    lines = [" ".join(html.unescape(_TAG.sub(" ", line)).replace("\xa0", " ").split()) for line in body.splitlines()]
    return "\n".join(line for line in lines if line)[:20_000]


class LinkedInSource:
    name = "linkedin"

    def __init__(self, request: Any, easy_apply_only: bool = False, delay_ms: int = 2_000,
                 sleep_ms: Callable[[int], None] | None = None, today: Callable[[], date] = date.today) -> None:
        self.request = request            # Playwright APIRequestContext (page.request)
        self.easy_apply_only = easy_apply_only
        self.delay_ms = delay_ms
        self.sleep_ms = sleep_ms or (lambda ms: None)
        self.today = today

    def search_url(self, query: Query, start: int) -> str:
        location = query.location if "india" in query.location.lower() else f"{query.location}, India"
        params: dict[str, Any] = {"keywords": query.keyword, "location": location, "start": start,
                                  "sortBy": "DD",                                   # most recent first
                                  "f_TPR": f"r{query.max_posted_days * 86_400}"}    # posted within N days
        if self.easy_apply_only:
            params["f_AL"] = "true"
        return f"{SEARCH_URL}?{urlencode(params)}"

    def _get(self, url: str) -> str:
        self.sleep_ms(self.delay_ms)
        response = self.request.get(url, timeout=30_000)
        for backoff in RATE_LIMIT_BACKOFF_MS:
            if response.status != 429:
                break
            self.sleep_ms(backoff)
            response = self.request.get(url, timeout=30_000)
        if response.status == 429:
            raise QueryBlocked("throttled", "HTTP 429")
        if response.status != 200:
            raise QueryBlocked("blocked", f"HTTP {response.status}")   # 999 = LinkedIn's bot check
        return response.text()

    def fetch_cards(self, query: Query, max_cards: int, max_pages: int) -> list[Card]:
        cards: list[Card] = []
        seen: set[str] = set()
        for page in range(max_pages):
            try:
                batch = parse_cards(self._get(self.search_url(query, page * PAGE_SIZE)), self.today())
            except QueryBlocked:
                if cards:
                    break          # keep the pages already fetched; only a first-page block fails the query
                raise
            for card in batch:
                if card.extra["job_id"] not in seen:
                    seen.add(card.extra["job_id"])
                    cards.append(card)
            if len(batch) < PAGE_SIZE or len(cards) >= max_cards:
                break
        return cards[:max_cards]

    def enrich(self, card: Card) -> Card:
        job_id = card.extra.get("job_id") or re.search(r"(\d{6,})/?(?:\?|$)", card.url).group(1)
        card.description = parse_description(self._get(POSTING_URL.format(job_id=job_id)))
        return card
