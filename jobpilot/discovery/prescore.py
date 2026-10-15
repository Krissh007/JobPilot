"""
Cheap pre-score for a listing card, 0-100. It only RANKS cards to decide which
get the enrichment budget; it never drops one (the Scoring agent judges).
"""

from __future__ import annotations

import re
from datetime import date

from jobpilot.discovery.cards import Card
from jobpilot.discovery.config import DiscoveryConfig
from jobpilot.discovery.titles import _norm


# Role family: the work you want vs. clearly different jobs that a broad variant title ("data analyst") also pulls in.
CORE_ROLE = re.compile(r"business (?:system |systems |data )?analyst|product (?:analyst|owner|manager)|\bbsa\b|systems? analyst|"
                       r"functional analyst|analytics|insights?", re.I)
OTHER_ROLE = re.compile(r"developer|engineer|tester|architect|\bsap\b|sales|devops|scientist|administrator|support|"
                        r"recruit|\bhr\b|mechanic|operator|technician|executive|associate for", re.I)


def role_adjustment(title: str) -> float:
    """+15 for a core BA/PA/PO/analytics title, -30 for an engineer/developer/sales-type title with none of those words."""
    if CORE_ROLE.search(title):
        return 15.0
    return -30.0 if OTHER_ROLE.search(title) else 0.0


def prescore(card: Card, config: DiscoveryConfig, today: date) -> float:
    title = f" {_norm(card.title)} "
    words = set(title.split())

    # Title relevance (0-60): a configured keyword beats a variant beats word overlap.
    best = 0.0
    for rank, terms in ((60.0, config.keywords), (50.0, config.variants)):
        for term in terms:
            t = _norm(term)
            if not t:
                continue
            if f" {t} " in title:
                best = max(best, rank)
            else:
                overlap = len(set(t.split()) & words) / len(t.split())
                best = max(best, rank * overlap * 0.6)
    score = best + role_adjustment(card.title)
    if words & {"ai", "genai", "llm"}:
        score += 10

    # Freshness (0-20); unknown age gets the middle.
    age = card.age_days(today)
    score += 10 if age is None else 20 * max(0.0, 1 - age / config.max_posted_days)

    # Experience overlap with the preferred range (0-10); unknown gets half.
    if card.exp_min is None:
        score += 5
    else:
        hi = card.exp_max if card.exp_max is not None else card.exp_min + 2
        score += 10 if card.exp_min <= config.exp_max and hi >= config.exp_min else 0

    return round(max(min(score, 100.0), 0.0), 1)
