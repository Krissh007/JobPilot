"""Registered alternative job titles, shared by Discovery (query variants) and Document Generation (title check)."""

from __future__ import annotations

REGISTERED_TITLES: tuple[str, ...] = (
    "AI business analyst",
    "AI product analyst",
    "product analyst",
    "business analyst",
    "data analyst",
    "analytics consultant",
    "insights analyst",
)


# Titles that mean the same kind of work as the configured keywords. Under strict_titles the portals are searched only
# for the keywords, but a card whose title contains one of these is kept too.
RELATED_TITLES: tuple[str, ...] = (
    "business systems analyst",
    "business system analyst",
    "business data analyst",
    "technical business analyst",
    "systems analyst",
    "system analyst",
    "functional analyst",
    "bsa",
    "business analysis",
    "associate product manager",
    "assistant product manager",
    "product owner",
    "product analytics",
    "business analytics",
    "business intelligence analyst",
    "requirements analyst",
    "product operations analyst",
    "ai solutions analyst",
    "ai business consultant",
)


def _norm(text: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


def expand_keywords(keywords: list[str], variants: list[str] | tuple[str, ...] = REGISTERED_TITLES) -> list[str]:
    """Configured keywords first, then registered variants not already covered. Order kept, case-insensitive dedupe."""
    seen: set[str] = set()
    out: list[str] = []
    for text in [*keywords, *variants]:
        key = _norm(text)
        if key and key not in seen:
            seen.add(key)
            out.append(text.strip())
    return out


def matches_registered_title(title: str, variants: list[str] | tuple[str, ...] = REGISTERED_TITLES) -> bool:
    """Whole-word containment, so 'Senior Business Analyst - Risk' matches but 'Analyst' alone does not."""
    padded = f" {_norm(title)} "
    return any(f" {_norm(v)} " in padded for v in variants)
