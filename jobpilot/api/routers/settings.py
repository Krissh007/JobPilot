"""Runtime settings (threshold, daily cap, search queries, schedule) stored in the settings table."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlmodel import Session

from jobpilot.api.deps import get_session
from jobpilot.db.models import DEFAULT_SETTINGS
from jobpilot.db.repo import get_setting, put_setting
from jobpilot.discovery.config import DiscoveryConfig
from jobpilot.scoring.gates import GateConfig

router = APIRouter(tags=["settings"])


DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
CLOCK = r"^([01]\d|2[0-3]):[0-5]\d$"


class Schedule(BaseModel):
    every_hours: int = Field(ge=1, le=24)
    start: str = Field(pattern=CLOCK)
    end: str = Field(pattern=CLOCK)
    timezone: str = "Asia/Kolkata"
    # Which days it runs (default: every day) and, optionally, exact run times that replace start/every/until.
    days: list[str] = Field(default_factory=lambda: list(DAYS))
    times: list[str] = Field(default_factory=list)

    @field_validator("days")
    @classmethod
    def _days(cls, value: list[str]) -> list[str]:
        if not value or set(value) - set(DAYS):
            raise ValueError("days must be a non-empty subset of mon..sun")
        return [d for d in DAYS if d in value]

    @field_validator("times")
    @classmethod
    def _times(cls, value: list[str]) -> list[str]:
        import re

        if any(not re.match(CLOCK, t) for t in value) or len(value) > 12:
            raise ValueError("times must be up to 12 HH:MM values")
        return sorted(set(value))


class DiscoveryPatch(BaseModel):
    """Search filters for the redesign pipeline. Anything omitted keeps its current value."""
    keywords: Optional[list[str]] = None
    use_variants: Optional[bool] = None
    variants: Optional[list[str]] = None
    locations: Optional[list[str]] = None
    sources: Optional[list[str]] = None
    max_posted_days: Optional[int] = None
    max_cards_per_query: Optional[int] = None
    max_pages_per_query: Optional[int] = None
    enrich_per_query: Optional[int] = None
    backlog_budget: Optional[int] = None
    naukri_sort_param: Optional[str] = None


class ScoringPatch(BaseModel):
    exp_min: Optional[int] = Field(None, ge=0, le=40)
    exp_max: Optional[int] = Field(None, ge=0, le=40)


class WeeklyReview(BaseModel):
    day: str = Field(pattern=r"^(mon|tue|wed|thu|fri|sat|sun)$")
    time: str = Field(pattern=r"^\d{2}:\d{2}$")


class SettingsPatch(BaseModel):
    threshold: Optional[int] = Field(None, ge=30, le=95)
    auto_submit: Optional[bool] = None
    linkedin_easy_apply_only: Optional[bool] = None
    review_wait_minutes: Optional[int] = Field(None, ge=5, le=120)
    weekly_review: Optional[WeeklyReview] = None
    daily_apply_cap: Optional[int] = Field(None, ge=1, le=40)
    search_queries: Optional[list[str]] = None
    schedule: Optional[Schedule] = None
    pipeline: Optional[Literal["legacy", "redesign"]] = None
    discovery: Optional[DiscoveryPatch] = None
    scoring: Optional[ScoringPatch] = None

    @field_validator("search_queries")
    @classmethod
    def _clean(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        cleaned = list(dict.fromkeys(q.strip() for q in value if q.strip()))
        if not cleaned:
            raise ValueError("at least one search query is required")
        return cleaned


def all_settings(session: Session) -> dict[str, Any]:
    out = {key: get_setting(session, key) for key in DEFAULT_SETTINGS}
    schedule = dict(out["schedule"])
    schedule.setdefault("days", list(DAYS))
    schedule.setdefault("times", [])
    out["schedule"] = schedule
    # The redesign filters are returned in full (stored overrides + defaults) so the dashboard can show every value.
    out["discovery"] = DiscoveryConfig.from_settings(session).model_dump()
    out["scoring"] = GateConfig(**(get_setting(session, "scoring") or {})).model_dump()
    return out


@router.get("/settings")
def read_settings(session: Session = Depends(get_session)) -> dict[str, Any]:
    return all_settings(session)


@router.put("/settings")
def update_settings(body: SettingsPatch, session: Session = Depends(get_session)) -> dict[str, Any]:
    for key, value in body.model_dump(exclude_none=True).items():
        if key in ("discovery", "scoring"):
            value = _merge_filters(session, key, value)
        put_setting(session, key, value)
    session.commit()
    return all_settings(session)


def _merge_filters(session: Session, key: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Stored overrides + this patch, validated by the model the agents load, so a bad filter is a 422 not a failed run."""
    merged = {**(get_setting(session, key) or {}), **patch}
    try:
        if key == "discovery":
            DiscoveryConfig(**merged)
        else:
            cfg = GateConfig(**merged)
            if cfg.exp_min > cfg.exp_max:
                raise ValueError("exp_min must not exceed exp_max")
    except (ValidationError, ValueError) as error:
        raise HTTPException(422, str(error).splitlines()[0][:200]) from error
    return merged
