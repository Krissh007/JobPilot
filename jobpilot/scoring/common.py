"""Pieces of the scoring prompt shared by Scoring (shortlist) and Discovery's backlog."""

from __future__ import annotations

from typing import Any, Protocol

from jobpilot.db.models import Job
from jobpilot.resume import Resume

MIN_JD_CHARS = 150
MAX_JD_CHARS = 12_000  # keeps each request small on the free tier


class JSONClient(Protocol):
    def generate_json(self, system: str, prompt: str) -> dict[str, Any]: ...


def build_prompt(job: Job, resume: Resume) -> str:
    exp = f"{job.exp_min}-{job.exp_max} years" if job.exp_min is not None else "not stated"
    return (
        f"CANDIDATE RESUME\n{resume.text}\n\n"
        f"JOB\nTitle: {job.title}\nCompany: {job.company}\nLocation: {job.location or 'not stated'}\n"
        f"Experience required: {exp}\n\nJOB DESCRIPTION\n{job.jd_text[:MAX_JD_CHARS]}"
    )
