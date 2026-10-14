"""
SQLModel tables. The schema itself is owned by Alembic (alembic/versions);
these classes must stay in step with it. Times are timezone-aware UTC.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from typing import Any, Optional

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, enum.Enum):
    found = "found"
    shortlisted = "shortlisted"
    docs_ready = "docs_ready"
    applied = "applied"
    interview = "interview"
    offer = "offer"
    rejected = "rejected"


class Actor(str, enum.Enum):
    user = "user"
    agent = "agent"


class AgentName(str, enum.Enum):
    search = "search"
    score = "score"
    docs = "docs"
    apply = "apply"


class RunStatus(str, enum.Enum):
    running = "running"
    ok = "ok"
    error = "error"


class ApplicationState(str, enum.Enum):
    filling = "filling"
    pending_review = "pending_review"
    approved = "approved"
    submitted = "submitted"
    skipped = "skipped"
    blocked = "blocked"


class FieldSource(str, enum.Enum):
    resume = "resume"
    answer_bank = "answer_bank"
    llm = "llm"


def enum_column(enum_cls: type[enum.Enum], name: str, **kwargs: Any) -> sa.Column:
    # Stored as VARCHAR + CHECK (non-native) so SQLite enforces the values.
    return sa.Column(
        sa.Enum(enum_cls, name=name, native_enum=False, create_constraint=True, validate_strings=True),
        **kwargs,
    )


class Job(SQLModel, table=True):
    __tablename__ = "jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    company: str
    source: str = ""
    url: str = Field(sa_column=sa.Column(sa.String, unique=True, nullable=False))
    url_canonical: str = Field(index=True)
    dedupe_hash: str = Field(index=True)
    location: str = ""
    exp_min: Optional[int] = None
    exp_max: Optional[int] = None
    jd_text: str = ""
    skills: list[str] = Field(default_factory=list, sa_column=sa.Column(sa.JSON, nullable=False, server_default="[]"))
    score: Optional[float] = Field(default=None, index=True)
    score_skills: Optional[float] = None
    score_experience: Optional[float] = None
    score_domain: Optional[float] = None
    score_reason: str = ""
    legacy_score: Optional[float] = None
    status: JobStatus = Field(
        default=JobStatus.found,
        sa_column=enum_column(JobStatus, "job_status", nullable=False, index=True, server_default="found"),
    )
    resume_version: str = ""
    resume_path: str = ""
    cover_letter_path: str = ""
    found_at: datetime = Field(default_factory=utcnow, index=True)
    posted_on: Optional[date] = None  # portal's posting date when known (Discovery)
    applied_at: Optional[datetime] = None
    notes: str = ""
    is_example: bool = False


class StatusHistory(SQLModel, table=True):
    __tablename__ = "status_history"

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(sa_column=sa.Column(sa.Integer, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True))
    from_status: Optional[JobStatus] = Field(default=None, sa_column=enum_column(JobStatus, "history_from_status", nullable=True))
    to_status: JobStatus = Field(sa_column=enum_column(JobStatus, "history_to_status", nullable=False))
    changed_at: datetime = Field(default_factory=utcnow)
    actor: Actor = Field(sa_column=enum_column(Actor, "history_actor", nullable=False))


class AgentRun(SQLModel, table=True):
    __tablename__ = "agent_runs"

    id: Optional[int] = Field(default=None, primary_key=True)
    agent: AgentName = Field(sa_column=enum_column(AgentName, "agent_name", nullable=False, index=True))
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    found: int = 0
    processed: int = 0
    status: RunStatus = Field(default=RunStatus.running, sa_column=enum_column(RunStatus, "run_status", nullable=False))
    error: str = ""
    log: str = ""
    is_example: bool = False


class Application(SQLModel, table=True):
    __tablename__ = "applications"

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(sa_column=sa.Column(sa.Integer, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True))
    state: ApplicationState = Field(
        default=ApplicationState.filling,
        sa_column=enum_column(ApplicationState, "application_state", nullable=False, index=True),
    )
    screenshot_path: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    decided_at: Optional[datetime] = None
    # Why it ended as it did: not_easy_apply, skipped_by_you, timeout, failed, blocked, tab_closed ...
    reason: str = ""
    is_example: bool = False


class ApplicationField(SQLModel, table=True):
    __tablename__ = "application_fields"

    id: Optional[int] = Field(default=None, primary_key=True)
    application_id: int = Field(
        sa_column=sa.Column(sa.Integer, sa.ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True)
    )
    step: int = 1
    field_name: str
    value_used: str = ""
    source: FieldSource = Field(sa_column=enum_column(FieldSource, "field_source", nullable=False))
    needs_review: bool = False


class Answer(SQLModel, table=True):
    __tablename__ = "answers"

    id: Optional[int] = Field(default=None, primary_key=True)
    question: str
    answer: str
    tags: str = ""
    times_used: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    is_example: bool = False


class Setting(SQLModel, table=True):
    __tablename__ = "settings"

    key: str = Field(primary_key=True)
    value: Any = Field(sa_column=sa.Column(sa.JSON, nullable=False))


class LLMCache(SQLModel, table=True):
    __tablename__ = "llm_cache"

    key: str = Field(primary_key=True)  # sha256(kind + jd_text + resume_version)
    kind: str
    response: Any = Field(sa_column=sa.Column(sa.JSON, nullable=False))
    created_at: datetime = Field(default_factory=utcnow)


DEFAULT_SETTINGS: dict[str, Any] = {
    "threshold": 60,
    "daily_apply_cap": 10,
    "search_queries": ["AI Business Analyst", "AI Product Analyst", "Business Analyst", "Product Analyst"],
    "schedule": {"every_hours": 4, "start": "08:00", "end": "22:00", "timezone": "Asia/Kolkata"},
    # Apply agent. Auto-submit stays off until you switch it on (Settings), and even then only for
    # docs_ready jobs >= threshold whose every field came from your resume/profile or answer bank.
    "auto_submit": False,
    "review_wait_minutes": 20,
    # Weekly review of the answer bank (open questions + questions forms asked that week).
    "weekly_review": {"day": "mon", "time": "09:00"},
    "answers_reviewed_at": None,
    # Search LinkedIn with its Easy Apply filter (other LinkedIn jobs can't be applied to by the agent).
    "linkedin_easy_apply_only": True,
    # Which agents the scheduled pass runs: "legacy" (search, score, docs) or "redesign" (discover, shortlist, documents).
    "pipeline": "legacy",
    # Apply agent: a form stuck on missing answers is saved for you to finish (True) or held open and waited on (False).
    "park_incomplete": True,
    # Partial overrides for the redesign agents (jobpilot.discovery.config.DiscoveryConfig, jobpilot.scoring.gates.GateConfig).
    "discovery": {},
    "scoring": {},
}
