"""
Score agent: Gemini rates each unscored JD against the master resume.

    python -m jobpilot.agents.score              # up to 20 unscored jobs, best legacy score first
    python -m jobpilot.agents.score --limit 5
    python -m jobpilot.agents.score --job-id 42  # (re)score one job

Output per job (validated with Pydantic, one retry on bad JSON):
{score, skills, experience, domain (all 0-100), reason, matched_skills, missing_skills}
Jobs scoring >= the threshold setting move found -> shortlisted.
agent_runs.found = jobs shortlisted, processed = jobs scored.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import func, nulls_last
from sqlmodel import Session, select

from jobpilot.agents._run import RunRecorder, agent_run
from jobpilot.db.models import Actor, AgentName, Job, JobStatus
from jobpilot.db.repo import get_setting, set_status
from jobpilot.db.session import get_engine
from jobpilot.llm.cache import cache_get, cache_key, cache_put
from jobpilot.llm.gemini import GeminiClient, LLMConfigError, LLMError, QuotaExhausted
from jobpilot.resume import Resume, load_resume

PROMPT_VERSION = "score:v1"  # bump when the prompt changes so cached results are not reused
MIN_JD_CHARS = 150
MAX_JD_CHARS = 12_000  # keeps each request small on the free tier

SYSTEM = """You are a strict technical recruiter screening jobs for one candidate.
Score how well the candidate's resume fits the job description. Use only facts in the resume;
never assume skills or experience it does not show.

Return JSON only, exactly this shape:
{"score": int 0-100, "skills": int 0-100, "experience": int 0-100, "domain": int 0-100,
 "reason": "one or two sentences, max 300 characters",
 "matched_skills": ["skill from the JD the resume demonstrates", ...],
 "missing_skills": ["skill the JD requires that the resume does not show", ...]}

- skills: overlap between required skills/tools and those the resume demonstrates.
- experience: fit of the candidate's years and seniority to the JD's range
  (candidate targets 2-4 years; far outside the JD's range scores low).
- domain: fit of industry and problem space (AI/ML products, analytics, business analysis).
- score: overall fit, roughly 0.45*skills + 0.30*experience + 0.25*domain, adjusted for hard
  blockers (e.g. a mandatory certification or 8+ years required).
Lists: at most 12 items each, short skill names."""


class ScoreResult(BaseModel):
    score: int = Field(ge=0, le=100)
    skills: int = Field(ge=0, le=100)
    experience: int = Field(ge=0, le=100)
    domain: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)

    @field_validator("reason")
    @classmethod
    def _trim_reason(cls, value: str) -> str:
        return " ".join(value.split())[:400]

    @field_validator("matched_skills", "missing_skills")
    @classmethod
    def _clean_list(cls, value: list[str]) -> list[str]:
        return [" ".join(str(item).split()) for item in value if str(item).strip()][:12]


class JSONClient(Protocol):
    def generate_json(self, system: str, prompt: str) -> dict[str, Any]: ...


def build_prompt(job: Job, resume: Resume) -> str:
    exp = f"{job.exp_min}-{job.exp_max} years" if job.exp_min is not None else "not stated"
    return (
        f"CANDIDATE RESUME\n{resume.text}\n\n"
        f"JOB\nTitle: {job.title}\nCompany: {job.company}\nLocation: {job.location or 'not stated'}\n"
        f"Experience required: {exp}\n\nJOB DESCRIPTION\n{job.jd_text[:MAX_JD_CHARS]}"
    )


def ask_with_retry(client: JSONClient, prompt: str) -> ScoreResult:
    """One call, plus one retry with the validation error spelled out."""
    try:
        return ScoreResult.model_validate(client.generate_json(SYSTEM, prompt))
    except (QuotaExhausted, LLMConfigError):
        raise
    except (LLMError, ValidationError) as error:
        problem = str(error).splitlines()[0][:300]
        retry = f"{prompt}\n\nYour previous reply was rejected: {problem}\nReturn only the JSON object described."
        return ScoreResult.model_validate(client.generate_json(SYSTEM, retry))


def apply_result(session: Session, job: Job, result: ScoreResult, resume: Resume, threshold: float) -> bool:
    """Write the score; returns True when the job was shortlisted by it."""
    job.score, job.score_skills = result.score, result.skills
    job.score_experience, job.score_domain = result.experience, result.domain
    job.score_reason, job.skills = result.reason, result.matched_skills
    job.resume_version = resume.version
    session.add(job)
    if job.status == JobStatus.found and result.score >= threshold:
        return set_status(session, job, JobStatus.shortlisted, actor=Actor.agent)
    return False


def select_jobs(session: Session, limit: int, job_id: int | None) -> list[Job]:
    if job_id is not None:
        job = session.get(Job, job_id)
        return [job] if job else []
    query = (
        select(Job)
        .where(Job.score.is_(None), Job.is_example.is_(False), func.length(Job.jd_text) >= MIN_JD_CHARS)
        .order_by(nulls_last(Job.legacy_score.desc()), Job.found_at.desc())
        .limit(limit)
    )
    return list(session.exec(query))


def score_jobs(session: Session, rec: RunRecorder, client: JSONClient | None, resume: Resume,
               limit: int = 20, job_id: int | None = None) -> None:
    threshold = float(get_setting(session, "threshold"))
    jobs = select_jobs(session, limit, job_id)
    no_jd = session.exec(select(func.count()).select_from(Job).where(
        Job.score.is_(None), Job.is_example.is_(False), func.length(Job.jd_text) < MIN_JD_CHARS)).one()
    rec.log(f"Resume {resume.path.name} (version {resume.version}); threshold {threshold:g}; "
            f"{len(jobs)} job(s) to score; {no_jd} unscored without a usable JD skipped")

    failed = 0
    for index, job in enumerate(jobs):
        key = cache_key(PROMPT_VERSION, job.jd_text, resume.version)
        cached = cache_get(session, key)
        if cached is None and client is None:
            client = GeminiClient()  # created only when needed; a config error ends the run
        try:
            if cached is not None:
                result, origin = ScoreResult.model_validate(cached), "cache"
            else:
                result, origin = ask_with_retry(client, build_prompt(job, resume)), "gemini"
                cache_put(session, key, PROMPT_VERSION, result.model_dump())
        except QuotaExhausted:
            session.commit()
            rec.log(f"Gemini quota exhausted; {len(jobs) - index} job(s) left for the next run")
            raise
        except LLMConfigError:
            session.commit()
            raise
        except (LLMError, ValidationError) as error:
            failed += 1
            rec.log(f"  #{job.id} {job.title} | {job.company}: FAILED after retry ({str(error).splitlines()[0][:160]})")
            continue

        shortlisted = apply_result(session, job, result, resume, threshold)
        session.commit()  # one job at a time, so an interrupted run keeps its work
        rec.processed += 1
        rec.found += shortlisted
        rec.log(f"  #{job.id} {result.score:>3} (S{result.skills}/E{result.experience}/D{result.domain}) "
                f"{job.title} | {job.company} [{origin}]{' -> shortlisted' if shortlisted else ''}")
    if failed:
        rec.log(f"{failed} job(s) could not be scored (invalid output after one retry); they stay unscored")


def run(limit: int = 20, job_id: int | None = None, db_url: str | None = None, client: JSONClient | None = None) -> None:
    with agent_run(AgentName.score, db_url) as rec:
        resume = load_resume()
        with Session(get_engine(db_url)) as session:
            score_jobs(session, rec, client, resume, limit, job_id)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=20, help="Max jobs this run (free-tier friendly; default 20)")
    parser.add_argument("--job-id", type=int, help="Score (or re-score) one job")
    args = parser.parse_args(argv)
    try:
        run(args.limit, args.job_id)
    except Exception:
        sys.exit(1)  # already recorded in agent_runs and printed


if __name__ == "__main__":
    main()
