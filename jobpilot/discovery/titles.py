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
