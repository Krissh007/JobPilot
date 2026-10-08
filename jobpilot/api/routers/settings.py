"""Runtime settings (threshold, daily cap, search queries, schedule) stored in the settings table."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session

from jobpilot.api.deps import get_session
from jobpilot.db.models import DEFAULT_SETTINGS
from jobpilot.db.repo import get_setting, put_setting

router = APIRouter(tags=["settings"])


class Schedule(BaseModel):
    every_hours: int = Field(ge=1, le=24)
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")
    timezone: str = "Asia/Kolkata"


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
    return {key: get_setting(session, key) for key in DEFAULT_SETTINGS}


@router.get("/settings")
def read_settings(session: Session = Depends(get_session)) -> dict[str, Any]:
    return all_settings(session)


@router.put("/settings")
def update_settings(body: SettingsPatch, session: Session = Depends(get_session)) -> dict[str, Any]:
    for key, value in body.model_dump(exclude_none=True).items():
        put_setting(session, key, value)
    session.commit()
    return all_settings(session)
