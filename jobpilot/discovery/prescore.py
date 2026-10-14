"""
Cheap pre-score for a listing card, 0-100. It only RANKS cards to decide which
get the enrichment budget; it never drops one (the Scoring agent judges).
"""

from __future__ import annotations

from datetime import date

from jobpilot.discovery.cards import Card
from jobpilot.discovery.config import DiscoveryConfig
from jobpilot.discovery.titles import _norm


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
    score = best
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

    return round(min(score, 100.0), 1)
