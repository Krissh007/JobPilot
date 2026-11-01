"""
Agent 2: Scoring & Shortlisting (the redesign). Runs alongside the old `score` agent, which stays runnable.

    python -m jobpilot.agents.shortlist               # gate every `found` job, Gemini-score up to 20
    python -m jobpilot.agents.shortlist --limit 5
    python -m jobpilot.agents.shortlist --dry-run     # show what the gates would reject; no Gemini, no writes

1. Hard gates (jobpilot.scoring.gates): India-only, experience overlapping 3-7 years. Failures move
   found -> rejected with the reason in notes. This is only a cheap pre-filter.
2. No rule-score floor: the legacy 45 floor is gone and legacy_score is ignored.
3. Gemini is the sole judge of everything that passes. score >= `threshold` (60) moves found -> shortlisted;
   below it the job stays `found` with its score set, so it is not scored twice.
4. Gemini places each job in an AI tier: core (no cap), adjacent = working on AI products/platforms or ML-driven
   analytics (no cap), non_ai (capped at 65 in code, not just in the prompt). The tier is stored in score_reason.
5. --job-ids re-scores exactly those jobs; a shortlisted job that drops below the threshold goes back to `found`
   (unless documents already exist).

agent_runs.found = jobs shortlisted, processed = jobs scored by Gemini.
"""

from __future__ import annotations

import argparse
import sys

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import func
from sqlmodel import Session, select

from jobpilot.agents._run import RunRecorder, agent_run
from jobpilot.discovery.prescore import role_adjustment
from jobpilot.scoring.common import MIN_JD_CHARS, JSONClient, build_prompt
from jobpilot.db.models import Actor, AgentName, Job, JobStatus
from jobpilot.db.repo import get_setting, set_status
from jobpilot.db.session import get_engine
from jobpilot.llm.cache import cache_get, cache_key, cache_put
from jobpilot.llm.gemini import GeminiClient, LLMConfigError, LLMError, QuotaExhausted
from jobpilot.resume import Resume, load_resume
from jobpilot.experience import experience_from_jd
from jobpilot.scoring.fit import FitConfig, classify
from jobpilot.scoring.gates import GATE_PREFIX, GateConfig, check_gates

PROMPT_VERSION = "shortlist:v2"  # v2: three AI tiers replace the ai_role yes/no
NON_AI_CAP = 65  # non-AI roles can still reach the shortlist (threshold 60) but never outrank a strong AI role
TIERS = ("core", "adjacent", "non_ai")
TIER_LABEL = {"core": "AI-core", "adjacent": "AI-adjacent", "non_ai": "non-AI"}
TIER_TAG = "[Tier: "  # score_reason ends with "[Tier: AI-core]"; the dashboard and tier_of() read it back

SYSTEM = """You are a strict recruiter screening jobs for one candidate (about 3-5 years of experience).
Score how well the candidate's resume fits the job description. Use only facts in the resume;
never assume skills or experience it does not show.

Return JSON only, exactly this shape:
{"score": int 0-100, "skills": int 0-100, "experience": int 0-100, "domain": int 0-100,
 "ai_tier": "core" | "adjacent" | "non_ai",
 "reason": "one or two sentences, max 300 characters",
 "matched_skills": ["skill from the JD the resume demonstrates", ...],
 "missing_skills": ["skill the JD requires that the resume does not show", ...]}

- ai_tier: how AI-related the role's OWN responsibilities and requirements are. Judge from those ONLY. Ignore
  portal metadata (e.g. "Department: Data Science & Analytics"), the "About company" section and taglines: a
  generic BA role at an AI company is still non_ai.
  "core"     = the work is building, evaluating or applying AI/ML/GenAI/LLM models, or data science / ML engineering.
  "adjacent" = the work is ON AI products or platforms (requirements, analysis, product ownership or delivery for
               an AI product, AI tooling or ML platform) or analytics that is driven by ML models (predictive or
               ML-based analytics, AI-assisted analysis as a main part of the job). Using AI tools for
               convenience does not count.
  "non_ai"   = neither: generic business, product, BI/reporting, data or functional analysis with no AI/ML work.
- skills: overlap between required skills/tools and those the resume demonstrates.
- experience: fit of the candidate's years and seniority to the JD's range (3-7 years is acceptable;
  far outside it scores low).
- domain: 85-100 role centres on AI/ML/GenAI/data-product work; 60-80 analytics-heavy or data-platform work
  with some AI/ML exposure; 40-60 generic business or product analysis; below 40 an unrelated specialism
  (SAP functional, Guidewire, insurance core systems, sales).
- score: overall fit, roughly 0.45*skills + 0.30*experience + 0.25*domain, adjusted for hard blockers
  (a mandatory certification, 8+ years required). A "non_ai" score is capped, so score honestly.
  The reason must name the AI/ML work in the responsibilities (and why it is core or adjacent), or say there is none.
Lists: at most 12 items each, short skill names."""


class ShortlistScore(BaseModel):
    score: int = Field(ge=0, le=100)
    skills: int = Field(ge=0, le=100)
    experience: int = Field(ge=0, le=100)
    domain: int = Field(ge=0, le=100)
    ai_tier: str
    reason: str = Field(min_length=1)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)

    @field_validator("ai_tier", mode="before")
    @classmethod
    def _tier(cls, value: object) -> str:
        key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        key = {"ai_core": "core", "ai_adjacent": "adjacent", "non_ai": "non_ai", "nonai": "non_ai", "none": "non_ai"}.get(key, key)
        if key not in TIERS:
            raise ValueError(f"ai_tier must be one of {', '.join(TIERS)}")
        return key

    @field_validator("reason")
    @classmethod
    def _trim_reason(cls, value: str) -> str:
        return " ".join(value.split())[:400]

    @field_validator("matched_skills", "missing_skills")
    @classmethod
    def _clean_list(cls, value: list[str]) -> list[str]:
        return [" ".join(str(item).split()) for item in value if str(item).strip()][:12]

    @property
    def final_score(self) -> int:
        return min(self.score, NON_AI_CAP) if self.ai_tier == "non_ai" else self.score


def ask_with_retry(client: JSONClient, prompt: str) -> ShortlistScore:
    try:
        return ShortlistScore.model_validate(client.generate_json(SYSTEM, prompt))
    except (QuotaExhausted, LLMConfigError):
        raise
    except (LLMError, ValidationError) as error:
        problem = str(error).splitlines()[0][:300]
        retry = f"{prompt}\n\nYour previous reply was rejected: {problem}\nReturn only the JSON object described."
        return ShortlistScore.model_validate(client.generate_json(SYSTEM, retry))


def tier_of(job: Job) -> str:
    """'AI-core' / 'AI-adjacent' / 'non-AI' from the tag written at scoring time, '' if never scored by this agent."""
    reason = job.score_reason or ""
    start = reason.rfind(TIER_TAG)
    return reason[start + len(TIER_TAG): reason.find("]", start)] if start >= 0 else ""


def apply_gates(session: Session, rec: RunRecorder, gates: GateConfig, dry_run: bool = False) -> int:
    """Reject unscored `found` jobs that fail a hard gate. Returns how many failed."""
    jobs = session.exec(select(Job).where(Job.status == JobStatus.found, Job.score.is_(None), Job.is_example.is_(False))).all()
    rejected: dict[str, int] = {}
    for job in jobs:
        if job.exp_min is None and job.exp_max is None and job.jd_text:  # re-check once a description exists
            job.exp_min, job.exp_max = experience_from_jd(job.jd_text)
            session.add(job)
        result = check_gates(job.location, job.exp_min, job.exp_max, gates, title=job.title)
        if result.passed:
            continue
        kind = ("location" if result.reason.startswith("not India")
                else "role" if result.reason.startswith("off-profile") else "experience")
        rejected[kind] = rejected.get(kind, 0) + 1
        rec.log(f"  gate{' (dry run)' if dry_run else ''}: #{job.id} {job.title} | {job.company}: {result.reason}")
        if not dry_run:
            job.notes = f"{job.notes}\n{GATE_PREFIX} {result.reason}".strip()
            set_status(session, job, JobStatus.rejected, actor=Actor.agent)
    if not dry_run:
        session.commit()
    total = sum(rejected.values())
    rec.log(f"Gates: {len(jobs)} checked, {total} rejected" + (f" ({', '.join(f'{k} {v}' for k, v in rejected.items())})" if total else ""))
    return total


def select_jobs(session: Session, limit: int, job_id: int | None, job_ids: list[int] | None = None) -> list[Job]:
    if job_ids:
        return [j for j in (session.get(Job, i) for i in job_ids) if j is not None and not j.is_example]
    if job_id is not None:
        job = session.get(Job, job_id)
        return [job] if job else []
    waiting = session.exec(
        select(Job)
        .where(Job.status == JobStatus.found, Job.score.is_(None), Job.is_example.is_(False),
               func.length(Job.jd_text) >= MIN_JD_CHARS)
        .order_by(Job.found_at.desc(), Job.id.desc())).all()  # id breaks ties: jobs saved in the same instant
    # Title relevance first (core BA/PA/PO titles, then neutral, then engineer/sales-type), newest first within a rank.
    # sorted() is stable, so the newest-first order above survives inside each rank.
    return sorted(waiting, key=lambda job: -role_adjustment(job.title))[:limit]


def apply_result(session: Session, job: Job, result: ShortlistScore, resume: Resume, threshold: float) -> bool:
    job.score, job.score_skills = result.final_score, result.skills
    job.score_experience, job.score_domain = result.experience, result.domain
    job.score_reason = (f"{result.reason}{' [Capped: non-AI]' if result.final_score != result.score else ''} "
                        f"{TIER_TAG}{TIER_LABEL[result.ai_tier]}]")
    job.skills = result.matched_skills
    job.resume_version = resume.version
    session.add(job)
    if job.status == JobStatus.found and result.final_score >= threshold:
        return set_status(session, job, JobStatus.shortlisted, actor=Actor.agent)
    if job.status == JobStatus.shortlisted and result.final_score < threshold and not job.resume_path:
        set_status(session, job, JobStatus.found, actor=Actor.agent)   # a re-score can take a job back out (never once documents exist)
    return False


def eliminate_after_scoring(session: Session, job: Job, gates: GateConfig, threshold: float, rec: RunRecorder) -> bool:
    """A similar title whose description, skills or experience do not fit is dropped (rejected, with the reason)."""
    fit = classify(job.title, job.exp_min, job.exp_max, job.score, job.score_skills, threshold,
                   FitConfig(exp_min=gates.exp_min, exp_max=gates.exp_max, min_skills=gates.min_skills))
    if fit.group != "eliminate" or job.status not in (JobStatus.found, JobStatus.shortlisted):
        return False
    job.notes = f"{job.notes}\n{GATE_PREFIX} unsuitable ({fit.reason})".strip()
    set_status(session, job, JobStatus.rejected, actor=Actor.agent)
    rec.log(f"      eliminated: {fit.reason}")
    return True


def shortlist_jobs(session: Session, rec: RunRecorder, client: JSONClient | None, resume: Resume,
                   limit: int = 20, job_id: int | None = None, dry_run: bool = False, job_ids: list[int] | None = None) -> None:
    gates = GateConfig(**(get_setting(session, "scoring") or {}))
    threshold = float(get_setting(session, "threshold"))
    apply_gates(session, rec, gates, dry_run)
    if dry_run:
        return
    jobs = select_jobs(session, limit, job_id, job_ids)
    no_jd = session.exec(select(func.count()).select_from(Job).where(
        Job.status == JobStatus.found, Job.score.is_(None), Job.is_example.is_(False),
        func.length(Job.jd_text) < MIN_JD_CHARS)).one()
    rec.log(f"Resume {resume.path.name} (version {resume.version}); threshold {threshold:g}; "
            f"{len(jobs)} job(s) to score; {no_jd} without a usable JD wait for enrichment")

    failed = 0
    for index, job in enumerate(jobs):
        key = cache_key(PROMPT_VERSION, job.jd_text, resume.version)
        cached = cache_get(session, key)
        if cached is None and client is None:
            client = GeminiClient(on_retry=rec.log)
        try:
            if cached is not None:
                result, origin = ShortlistScore.model_validate(cached), "cache"
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
        if gates.require_title_fit and eliminate_after_scoring(session, job, gates, threshold, rec):
            shortlisted = False
        session.commit()
        rec.processed += 1
        rec.found += shortlisted
        capped = " capped" if result.final_score != result.score else ""
        rec.log(f"  #{job.id} {result.final_score:>3}{capped} [{TIER_LABEL[result.ai_tier]}] (S{result.skills}/E{result.experience}/D{result.domain}) "
                f"{job.title} | {job.company} [{origin}]{' -> shortlisted' if shortlisted else ''}")
    if failed:
        rec.log(f"{failed} job(s) could not be scored (invalid output after one retry); they stay unscored")


def run(limit: int = 60, job_id: int | None = None, db_url: str | None = None, client: JSONClient | None = None,
        run_id: int | None = None, dry_run: bool = False, job_ids: list[int] | None = None) -> None:
    with agent_run(AgentName.scoring, db_url, run_id) as rec:
        resume = None if dry_run else load_resume()
        with Session(get_engine(db_url)) as session:
            shortlist_jobs(session, rec, client, resume, limit, job_id, dry_run, job_ids)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=60, help="Max Gemini calls this run (free-tier friendly)")
    parser.add_argument("--job-id", type=int, help="Score (or re-score) one job")
    parser.add_argument("--job-ids", type=int, nargs="+", help="Re-score exactly these jobs (e.g. after a prompt change)")
    parser.add_argument("--dry-run", action="store_true", help="Report gate rejections only; no Gemini, no writes")
    parser.add_argument("--run-id", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        run(args.limit, args.job_id, run_id=args.run_id, dry_run=args.dry_run, job_ids=args.job_ids)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
