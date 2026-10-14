from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from jobpilot.discovery.cards import Card


class QueryBlocked(Exception):
    """CAPTCHA, login wall or throttling: this query is abandoned, the run continues."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}{': ' + detail if detail else ''}")
        self.kind = kind


@dataclass(frozen=True)
class Query:
    source: str
    keyword: str
    location: str
    max_posted_days: int


class JobSource(Protocol):
    name: str

    def fetch_cards(self, query: Query, max_cards: int, max_pages: int) -> list[Card]:
        """Listing cards only, newest first where the portal can sort. Raises QueryBlocked or any error to fail the query."""

    def enrich(self, card: Card) -> Card:
        """Open the full description. Returns the card with description set."""
