"""
Role fit, in the order you set: job title first (and similar titles), total experience second, and for similar titles
the description, skills and experience decide whether a job is worth showing.

    exact    the title is the kind of role you search for (Business Analyst, Product Analyst, Product Owner, ...)
    similar  a neighbouring title (Data Analyst, Product Manager, Business Consultant, ...): only kept when the
             description fits, in a separate "You might like" section
    other    anything else (developer, tester, marketing, ...)

Groups returned by `classify` (computed from the stored job, so they follow the settings and never go stale):

    best        exact title, experience inside your range (or not stated), scored at or above the threshold
    maybe       similar title that the description supports: skills fit, scored at or above the threshold,
                experience inside your range (or not stated)  -> "You might like"
    pending     title fits but it is not scored yet
    below       title fits but scored under the threshold (stays unshortlisted)
    eliminate   title is neither, experience is outside your range, or a similar title whose skills do not fit
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel

# The roles you search for. Whole-phrase match on the normalised title.
EXACT_TITLES: tuple[str, ...] = (
    "ai business analyst", "ai product analyst", "business analyst", "product analyst", "business systems analyst",
    "business system analyst", "technical business analyst", "systems analyst", "system analyst", "functional analyst",
    "bsa", "associate product manager", "assistant product manager", "product owner", "technical product owner",
    "business analysis", "business data analyst", "requirements analyst", "ai solutions analyst",
)

# Neighbouring titles: worth a look only when the description fits.
SIMILAR_TITLES: tuple[str, ...] = (
    "data analyst", "data analytics", "business intelligence analyst", "bi analyst", "analytics consultant",
    "insights analyst", "product analytics", "business analytics", "product manager", "product operations",
    "product specialist", "product associate", "business consultant", "ai business consultant", "business execution consultant",
    "implementation consultant", "consulting analyst", "transformation", "program analyst", "process analyst",
    "strategy analyst", "scrum master", "business insights", "reporting analyst", "product delivery analyst",
    "data analysis", "data strategy", "ai strategy", "process intelligence", "mis analyst",
)

# A title with none of the above can still be a neighbour when it pairs an analyst/consultant word with a business one.
_NEIGHBOUR = re.compile(r"\b(?:analyst|analytics|consultant|product|business|insights?)\b", re.I)
# Never a fit, whatever else the title says ("Data Analytic | Fresher", "Business Analyst Training").
_NEVER = re.compile(r"\b(?:intern|interns|internship|trainee|fresher|freshers|training|entry level|graduates|annotation|annotator)\b", re.I)
_NOT_THIS_WORK = re.compile(
    r"\b(?:developer|engineer|tester|testing|qa|architect|devops|sre|scientist|administrator|designer|recruiter|"
    r"sales|marketing|accountant|auditor|audit|assurance|support)\b", re.I)


def _norm(text: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in (text or "").lower()).split())


def _contains(title: str, phrases: tuple[str, ...] | list[str]) -> bool:
    padded = f" {_norm(title)} "
    return any(f" {_norm(p)} " in padded for p in phrases if _norm(p))


class FitConfig(BaseModel):
    exp_min: int = 2
    exp_max: int = 6
    min_skills: int = 60            # similar titles need at least this skills sub-score to be shown
    exact_titles: list[str] = list(EXACT_TITLES)
    similar_titles: list[str] = list(SIMILAR_TITLES)


def title_fit(title: str, config: FitConfig | None = None) -> str:
    config = config or FitConfig()
    if _NEVER.search(title or ""):
        return "other"
    if _contains(title, config.exact_titles):
        return "exact"
    if _contains(title, config.similar_titles):
        return "similar"
    if _NEIGHBOUR.search(title or "") and not _NOT_THIS_WORK.search(title or ""):
        return "similar"
    return "other"


def experience_fit(exp_min: int | None, exp_max: int | None, config: FitConfig | None = None) -> str:
    """'in' when the stated range overlaps yours, 'unknown' when nothing is stated, else 'out'."""
    config = config or FitConfig()
    if exp_min is None and exp_max is None:
        return "unknown"
    low = exp_min if exp_min is not None else exp_max
    if exp_max is not None and exp_max < config.exp_min:
        return "out"
    if low is not None and low > config.exp_max:
        return "out"
    return "in"


@dataclass(frozen=True)
class Fit:
    group: str
    title: str
    experience: str
    reason: str


def classify(title: str, exp_min: int | None, exp_max: int | None, score: float | None, skills: float | None,
             threshold: float, config: FitConfig | None = None) -> Fit:
    config = config or FitConfig()
    kind = title_fit(title, config)
    exp = experience_fit(exp_min, exp_max, config)
    if kind == "other":
        return Fit("eliminate", kind, exp, "title is not a business/product analyst-type role")
    if exp == "out":
        span = f"{exp_min}-{exp_max}" if exp_max is not None else f"{exp_min}+"
        return Fit("eliminate", kind, exp, f"experience {span} yrs is outside {config.exp_min}-{config.exp_max}")
    if score is None:
        return Fit("pending", kind, exp, "not scored yet")
    if kind == "exact":
        return Fit("best", kind, exp, "role title matches") if score >= threshold else Fit("below", kind, exp, f"scored {score:g}, under {threshold:g}")
    # similar title: the description, skills and experience decide
    if score < threshold:
        return Fit("eliminate", kind, exp, f"similar title, scored {score:g} (under {threshold:g})")
    if skills is not None and skills < config.min_skills:
        return Fit("eliminate", kind, exp, f"similar title, skills fit only {skills:g} (needs {config.min_skills})")
    return Fit("maybe", kind, exp, "similar title; description, skills and experience fit")


@dataclass(frozen=True)
class FitContext:
    enabled: bool
    config: FitConfig
    threshold: float


def load_fit(session) -> FitContext:
    """Fit settings: the `scoring` setting (exp_min, exp_max, min_skills, require_title_fit) and the shortlist threshold."""
    from jobpilot.db.repo import get_setting

    stored = get_setting(session, "scoring") or {}
    config = FitConfig(**{k: stored[k] for k in ("exp_min", "exp_max", "min_skills") if k in stored})
    return FitContext(bool(stored.get("require_title_fit", False)), config, float(get_setting(session, "threshold")))


def group_of(job, context: FitContext) -> Fit:
    """Group of a stored job under the current settings."""
    return classify(job.title, job.exp_min, job.exp_max, job.score, job.score_skills, context.threshold, context.config)
