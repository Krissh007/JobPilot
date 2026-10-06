"""
Docs agent: an ATS-friendly tailored resume + cover letter (.docx) for each
shortlisted job, written to output/<Company>_<job_id>/.

    python -m jobpilot.agents.docs              # up to 10 shortlisted jobs without documents, best score first
    python -m jobpilot.agents.docs --limit 3
    python -m jobpilot.agents.docs --job-id 161 # (re)generate for one job

Reuses the legacy agent's tailoring unchanged (truth-first prompt, fact check
that strips unsupported skills/companies/institutions/new numbers, ATS .docx
writer). JobPilot adds:
- a date/title check: every role's dates must be the master resume's dates
  for that company (corrected if not), and every title must exist in it;
- its rate-limited Gemini client and the sha256 cache (a rerun costs no call);
- status shortlisted -> docs_ready (actor agent) once both files exist.
Never invents experience: only re-orders and re-phrases the master resume.
agent_runs.found = jobs that got documents, processed = jobs attempted.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, nulls_last
from sqlmodel import Session, select

from jobpilot.agents._run import RunRecorder, agent_run
from jobpilot.config import ROOT
from jobpilot.db.models import Actor, AgentName, Job, JobStatus
from jobpilot.db.repo import set_status
from jobpilot.db.session import get_engine
from jobpilot.legacy import bridge
from jobpilot.llm.cache import cache_get, cache_key, cache_put
from jobpilot.llm.gemini import GeminiClient, LLMConfigError, LLMError, QuotaExhausted
from jobpilot.resume import Resume, load_resume

PROMPT_VERSION = "docs:v1"
OUTPUT_DIR = ROOT / "output"
MIN_JD_CHARS = 300
DATE_RANGE = re.compile(
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}\s*[–—-]\s*"
    r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}|Present|Current)",
    re.IGNORECASE,
)


class DocsError(RuntimeError):
    """This job's documents could not be made truthfully; nothing was written."""


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def master_dates_for(company: str, master: str) -> list[str]:
    """Date ranges written next to this company in the master resume (its line + the 3 after)."""
    lines = master.splitlines()
    target = _norm(company)
    words = [w for w in target.split() if w not in {"pvt", "ltd", "limited", "private", "inc", "llp"}]
    found: list[str] = []
    for i, line in enumerate(lines):
        normalized = _norm(line)
        if target and (target in normalized or (words and all(w in normalized.split() for w in words))):
            for nearby in lines[i : i + 4]:
                found += [m.group(0) for m in DATE_RANGE.finditer(nearby)]
    return list(dict.fromkeys(found))


def verify_titles_and_dates(application: Any, master: str) -> list[str]:
    """Correct dates the model changed; refuse titles the master resume doesn't have."""
    notes: list[str] = []
    master_norm = _norm(master)
    for entry in application.experience:
        if entry.title and _norm(entry.title) not in master_norm:
            raise DocsError(f"Title '{entry.title}' at {entry.company} is not in the master resume")
        if entry.dates and _norm(entry.dates) in master_norm:
            continue
        options = master_dates_for(entry.company, master)
        if len(options) == 1:
            notes.append(f"Corrected dates for {entry.company}: '{entry.dates or '(none)'}' -> '{options[0]}'")
            entry.dates = options[0]
        else:
            raise DocsError(f"Could not verify the dates '{entry.dates}' for {entry.company} against the master resume")
    return notes


def select_jobs(session: Session, limit: int, job_id: int | None) -> list[Job]:
    if job_id is not None:
        job = session.get(Job, job_id)
        return [job] if job else []
    query = (
        select(Job)
        .where(Job.status == JobStatus.shortlisted, Job.is_example.is_(False), func.length(Job.jd_text) >= MIN_JD_CHARS)
        .order_by(nulls_last(Job.score.desc()), Job.found_at.desc())
    )
    pending = [j for j in session.exec(query) if not (j.resume_path and Path(j.resume_path).is_file()
                                                      and j.cover_letter_path and Path(j.cover_letter_path).is_file())]
    return pending[:limit]


def contact_details() -> dict[str, str]:
    """Name/email/phone/location from the legacy application_profile.yaml (git-ignored)."""
    path = bridge.application_profile_path()
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {k: str(v or "").strip() for k, v in (data.get("contact") or {}).items()}


def generate_one(job: Job, resume: Resume, client: Any, session: Session, log) -> dict[str, Any]:
    tailor, docx_writer, generator = bridge.tailoring()
    legacy_job = {"title": job.title, "company": job.company, "location": job.location, "description": job.jd_text,
                  "experience": f"{job.exp_min}-{job.exp_max} years" if job.exp_min is not None else "",
                  "job_url": job.url, "match_score": job.score, "match_category": ""}

    key = cache_key(PROMPT_VERSION, job.jd_text, resume.version)
    raw = cache_get(session, key)
    origin = "cache"
    if raw is None:
        raw = client.generate_json(tailor.SYSTEM_PROMPT, tailor.build_prompt(legacy_job, resume.text))
        origin = "gemini"
    try:
        application = tailor.TailoredApplication.model_validate(raw)
    except Exception as error:
        raise DocsError(f"Model output did not match the resume structure: {str(error)[:200]}") from error
    if origin == "gemini":
        cache_put(session, key, PROMPT_VERSION, raw)  # cache the raw answer; checks re-run every time

    application = tailor.fact_check(application, resume.text, extra_evidence=None)
    notes = verify_titles_and_dates(application, resume.text)
    if not application.experience:
        raise DocsError("No verifiable experience left after the fact check")
    if len(application.cover_letter.paragraphs) < 2:
        raise DocsError("Cover letter too short after the fact check")
    application = tailor.score_keywords(application)
    application.fact_check_warnings += notes

    contact = contact_details()
    if contact.get("full_name"):
        application.candidate.name = contact["full_name"]
    parts = [contact.get(k, "") for k in ("email", "phone", "current_location", "linkedin") if contact.get(k)]
    if len(parts) >= 2:
        application.candidate.contact_line = " | ".join(parts)

    slug = generator.slug
    folder = OUTPUT_DIR / f"{slug(job.company, 30)}_{job.id}"
    person = slug(application.candidate.name or "Candidate", 30)
    resume_file = docx_writer.write_resume(application, folder / f"{person}_Resume_{slug(job.company, 30)}.docx", job.title)
    letter_file = docx_writer.write_cover_letter(application, folder / f"{person}_Cover_Letter_{slug(job.company, 30)}.docx",
                                                 job.company, job.title)
    generator.DocumentGenerator._write_report({**legacy_job, "job_url": job.url}, application, folder / "match_report.md")
    return {"resume": resume_file, "cover": letter_file, "coverage": application.keyword_coverage,
            "warnings": application.fact_check_warnings, "missing": application.missing_keywords, "origin": origin}


def make_documents(session: Session, rec: RunRecorder, client: Any, resume: Resume, limit: int = 10,
                   job_id: int | None = None) -> None:
    jobs = select_jobs(session, limit, job_id)
    rec.log(f"Resume {resume.path.name} (version {resume.version}); {len(jobs)} job(s) need documents")
    failures = 0
    for job in jobs:
        rec.processed += 1
        needs_client = cache_get(session, cache_key(PROMPT_VERSION, job.jd_text, resume.version)) is None
        if needs_client and client is None:
            client = GeminiClient(on_retry=rec.log)  # only when a call is needed; config errors end the run
        try:
            out = generate_one(job, resume, client, session, rec.log)
        except (QuotaExhausted, LLMConfigError):
            session.commit()
            rec.log(f"Stopping: Gemini quota/config problem; {len(jobs) - rec.processed + 1} job(s) left for the next run")
            raise
        except (DocsError, LLMError, OSError) as error:
            failures += 1
            session.rollback()
            rec.log(f"  #{job.id} {job.title} | {job.company}: SKIPPED, {error}")
            if failures >= 3 and not job_id:
                rec.log("Stopping after 3 failures in a row")
                break
            continue
        failures = 0
        job.resume_path, job.cover_letter_path, job.resume_version = str(out["resume"]), str(out["cover"]), resume.version
        session.add(job)
        moved = job.status == JobStatus.shortlisted and set_status(session, job, JobStatus.docs_ready, actor=Actor.agent)
        session.commit()
        rec.found += 1
        rec.log(f"  #{job.id} {job.title} | {job.company} [{out['origin']}] -> {out['resume'].parent.name}/ "
                f"(ATS keyword coverage {out['coverage']}%, {len(out['warnings'])} fact-check fix(es), "
                f"{len(out['missing'])} JD gap(s)){' -> docs_ready' if moved else ''}")
        for warning in out["warnings"][:5]:
            rec.log(f"      fact check: {warning[:180]}")


def run(limit: int = 10, job_id: int | None = None, db_url: str | None = None, client: Any = None,
        run_id: int | None = None) -> None:
    with agent_run(AgentName.docs, db_url, run_id) as rec:
        resume = load_resume()
        with Session(get_engine(db_url)) as session:
            make_documents(session, rec, client, resume, limit, job_id)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=10, help="Max jobs this run (default 10)")
    parser.add_argument("--job-id", type=int, help="Generate (or regenerate) documents for one job")
    parser.add_argument("--run-id", type=int, help=argparse.SUPPRESS)  # set by the dashboard API
    args = parser.parse_args(argv)
    try:
        run(args.limit, args.job_id, run_id=args.run_id)
    except Exception:
        sys.exit(1)  # already recorded in agent_runs and printed


if __name__ == "__main__":
    main()
