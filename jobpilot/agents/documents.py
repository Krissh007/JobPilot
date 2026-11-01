"""
Agent 3: Document Generation.

    python -m jobpilot.agents.documents              # up to 10 shortlisted jobs without documents, best score first
    python -m jobpilot.agents.documents --limit 3
    python -m jobpilot.agents.documents --job-id 161

Engine (truth-first prompt, fact check that strips any skill, employer, institution
or number the master resume does not contain, date correction, ATS .docx writer, sha256 cache), plus:

- Skip when the JOB's title is unknown (empty or a placeholder like "(untitled)").
- Skip when the cover letter comes out too short (under 2 paragraphs or MIN_COVER_WORDS words) after the fact check.
- Registered alternative titles (jobpilot.discovery.titles) are accepted as role titles, so a valid role is not
  skipped just because the model used e.g. "Product Analyst" for a role the master resume titles
  "Business Analyst | AI Product Analyst". Each such use is written to the fact-check warnings.
  Any other title missing from the master resume still skips the job.
Status shortlisted -> docs_ready (actor agent) once both files exist.
agent_runs.found = jobs that got documents, processed = jobs attempted.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import func, nulls_last
from sqlmodel import Session, select

from jobpilot.agents._run import RunRecorder, agent_run
from jobpilot.db.models import Actor, AgentName, Job, JobStatus
from jobpilot.db.repo import set_status
from jobpilot.db.session import get_engine
from jobpilot.discovery.titles import REGISTERED_TITLES
from jobpilot.llm.cache import cache_get, cache_key, cache_put
from jobpilot.llm.gemini import GeminiClient, LLMConfigError, LLMError, QuotaExhausted
from jobpilot.resume import Resume, load_resume
from jobpilot.scoring.fit import group_of, load_fit
from jobpilot.tailoring import checks

PROMPT_VERSION = checks.PROMPT_VERSION
MIN_JD_CHARS = checks.MIN_JD_CHARS
MIN_COVER_PARAGRAPHS = 2
MIN_COVER_WORDS = 120  # the prompt asks for 250-350; below this the letter is a stub
UNKNOWN_TITLES = {"", "untitled", "unknown", "n a", "na", "none", "null"}

DocsError = checks.DocsError


def job_title_known(title: str) -> bool:
    return checks.norm(title or "") not in UNKNOWN_TITLES


def is_registered_title(title: str, aliases: tuple[str, ...] | list[str] = REGISTERED_TITLES) -> bool:
    """Whole title (or each part of 'A | B' / 'A / B') equals a registered alternative title."""
    known = {checks.norm(a) for a in aliases}
    parts = [p for p in (checks.norm(x) for x in title.replace("/", "|").split("|")) if p]
    return bool(parts) and all(p in known for p in parts)


def verify_titles_and_dates(application: Any, master: str, aliases: tuple[str, ...] | list[str] = REGISTERED_TITLES) -> list[str]:
    """Like checks.verify_titles_and_dates, but a registered alternative title is a valid title."""
    notes: list[str] = []
    master_norm = checks.norm(master)
    for entry in application.experience:
        if entry.title and checks.norm(entry.title) not in master_norm:
            if not is_registered_title(entry.title, aliases):
                raise DocsError(f"Title '{entry.title}' at {entry.company} is not in the master resume")
            notes.append(f"Title '{entry.title}' at {entry.company} accepted as a registered alternative title")
        if entry.dates and checks.norm(entry.dates) in master_norm:
            continue
        options = checks.master_dates_for(entry.company, master)
        if len(options) == 1:
            notes.append(f"Corrected dates for {entry.company}: '{entry.dates or '(none)'}' -> '{options[0]}'")
            entry.dates = options[0]
        else:
            raise DocsError(f"Could not verify the dates '{entry.dates}' for {entry.company} against the master resume")
    return notes


def cover_letter_too_short(application: Any) -> str:
    paragraphs = [p for p in application.cover_letter.paragraphs if p.strip()]
    words = sum(len(p.split()) for p in paragraphs)
    if len(paragraphs) < MIN_COVER_PARAGRAPHS or words < MIN_COVER_WORDS:
        return f"Cover letter too short after the fact check ({len(paragraphs)} paragraph(s), {words} words)"
    return ""


def select_jobs(session: Session, limit: int, job_id: int | None) -> list[Job]:
    if job_id is not None:
        job = session.get(Job, job_id)
        return [job] if job else []
    query = (select(Job)
             .where(Job.status == JobStatus.shortlisted, Job.is_example.is_(False), func.length(Job.jd_text) >= MIN_JD_CHARS)
             .order_by(nulls_last(Job.score.desc()), Job.found_at.desc()))
    context = load_fit(session)
    pending = [j for j in session.exec(query) if not (j.resume_path and Path(j.resume_path).is_file()
                                                      and j.cover_letter_path and Path(j.cover_letter_path).is_file())
               and (not context.enabled or group_of(j, context).group == "best")]   # "You might like" jobs wait for you
    return pending[:limit]


def generate_one(job: Job, resume: Resume, client: Any, session: Session, log) -> dict[str, Any]:
    if not job_title_known(job.title):
        raise DocsError("Job title is unknown")
    from jobpilot.tailoring import docx_writer, generator, tailor
    legacy_job = {"title": job.title, "company": job.company, "location": job.location, "description": job.jd_text,
                  "experience": f"{job.exp_min}-{job.exp_max} years" if job.exp_min is not None else "",
                  "job_url": job.url, "match_score": job.score, "match_category": ""}

    key = cache_key(PROMPT_VERSION, job.jd_text, resume.version)
    raw = cache_get(session, key)
    origin = "cache"
    if raw is None:
        prompt = tailor.build_prompt(legacy_job, resume.text)
        try:
            raw = client.generate_json(tailor.SYSTEM_PROMPT, prompt)
        except (QuotaExhausted, LLMConfigError):
            raise
        except LLMError as error:
            log(f"      retrying once: {str(error).splitlines()[0][:120]}")
            retry = f"{prompt}\n\nYour previous reply was not valid JSON ({error}). Return only the JSON object."
            raw = client.generate_json(tailor.SYSTEM_PROMPT, retry)
        origin = "gemini"
    try:
        application = tailor.TailoredApplication.model_validate(raw)
    except Exception as error:
        raise DocsError(f"Model output did not match the resume structure: {str(error)[:200]}") from error
    if origin == "gemini":
        cache_put(session, key, PROMPT_VERSION, raw)  # the raw answer is cached; every check below re-runs

    application = tailor.fact_check(application, resume.text, extra_evidence=None)
    notes = verify_titles_and_dates(application, resume.text)
    if not application.experience:
        raise DocsError("No verifiable experience left after the fact check")
    problem = cover_letter_too_short(application)
    if problem:
        raise DocsError(problem)
    application = tailor.score_keywords(application)
    application.fact_check_warnings += notes

    contact = checks.contact_details()
    if contact.get("full_name"):
        application.candidate.name = contact["full_name"]
    parts = [contact.get(k, "") for k in ("email", "phone", "current_location", "linkedin") if contact.get(k)]
    if len(parts) >= 2:
        application.candidate.contact_line = " | ".join(parts)

    slug = generator.slug
    folder = checks.OUTPUT_DIR / f"{slug(job.company, 30)}_{job.id}"
    person = slug(application.candidate.name or "Candidate", 30)
    short_title = job.title if len(job.title) <= 120 else job.title[:117].rstrip() + "..."
    resume_file = docx_writer.write_resume(application, folder / f"{person}_Resume_{slug(job.company, 30)}.docx", short_title)
    letter_file = docx_writer.write_cover_letter(application, folder / f"{person}_Cover_Letter_{slug(job.company, 30)}.docx",
                                                 job.company, short_title)
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
        if needs_client and client is None and job_title_known(job.title):
            client = GeminiClient(on_retry=rec.log)
        try:
            out = generate_one(job, resume, client, session, rec.log)
        except (QuotaExhausted, LLMConfigError):
            session.commit()
            rec.log(f"Stopping: Gemini quota/config problem; {len(jobs) - rec.processed + 1} job(s) left for the next run")
            raise
        except Exception as error:  # one bad job must not stop the run
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
    with agent_run(AgentName.documents, db_url, run_id) as rec:
        resume = load_resume()
        with Session(get_engine(db_url)) as session:
            make_documents(session, rec, client, resume, limit, job_id)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=10, help="Max jobs this run (default 10)")
    parser.add_argument("--job-id", type=int, help="Generate (or regenerate) documents for one job")
    parser.add_argument("--run-id", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        run(args.limit, args.job_id, run_id=args.run_id)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
