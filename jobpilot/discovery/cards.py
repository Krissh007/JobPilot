"""A listing card: what a portal's search results page shows, before opening the full description."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from jobpilot.db.dedupe import canonical_url, dedupe_hash


@dataclass
class Card:
    title: str
    company: str
    url: str
    source: str
    location: str = ""
    posted_text: str = ""        # portal's raw text ("3 days ago") or an ISO date
    posted_on: date | None = None
    experience_text: str = ""
    exp_min: int | None = None
    exp_max: int | None = None
    snippet: str = ""            # the card's short description, if the portal shows one
    description: str = ""        # full JD, set only by enrichment
    query: str = ""
    prescore: float = 0.0
    enriched: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return canonical_url(self.url)

    @property
    def hash(self) -> str:
        return dedupe_hash(self.company, self.title, self.location)

    def age_days(self, today: date) -> int | None:
        return None if self.posted_on is None else max(0, (today - self.posted_on).days)


_RELATIVE = re.compile(r"(\d+)\+?\s*(minute|min|hour|day|week|month)s?\s+ago", re.I)
_UNIT_DAYS = {"minute": 0, "min": 0, "hour": 0, "day": 1, "week": 7, "month": 30}


def parse_posted(text: str, today: date) -> date | None:
    """Absolute date from an ISO date or relative portal text; None when the portal shows nothing usable."""
    value = (text or "").strip().lower()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        pass
    if any(t in value for t in ("just now", "just posted", "today", "few hours")):
        return today
    m = _RELATIVE.search(value)
    if m:
        return today - timedelta(days=int(m.group(1)) * _UNIT_DAYS[m.group(2).lower()])
    return None


_EXP = re.compile(r"(\d+)\s*(?:-|to|–)\s*(\d+)")
_EXP_ONE = re.compile(r"(\d+)\s*\+?\s*(?:yrs?|years?)", re.I)


def parse_experience(text: str) -> tuple[int | None, int | None]:
    m = _EXP.search(text or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _EXP_ONE.search(text or "")
    if m:
        return int(m.group(1)), None
    return None, None
