"""
Pick the jobs worth applying to (match score >= threshold), and write a
tailored resume, cover letter and keyword report for each one.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from typing import Any as Database  # the legacy Database type; only duck-typed here
from jobpilot.tailoring.docx_writer import write_cover_letter, write_resume
from jobpilot.tailoring.llm import LLMClient, LLMError
from jobpilot.tailoring.master_resume import load_master_resume
from jobpilot.tailoring.tailor import TailoredApplication, tailor_application

logger = logging.getLogger(__name__)

CLOSED_STATUSES = {"rejected", "skipped", "applied", "interviewing", "offer"}


@dataclass
class GenerationResult:
    created: list[dict[str, Any]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    below_threshold: int = 0
    already_done: int = 0
    no_description: int = 0
    deferred: int = 0

    def log_summary(self) -> None:
        logger.info(
            "Tailored documents: created %s, failed %s, already done %s, "
            "below threshold %s, no description %s, deferred to next run %s",
            len(self.created), len(self.failed), self.already_done,
            self.below_threshold, self.no_description, self.deferred,
        )
        for url, error in self.failed:
            logger.warning("Tailoring failed for %s: %s", url, error)


def slug(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", str(text or "")).strip("_")
    return cleaned[:limit].rstrip("_") or "Unknown"


def _load_contact(profile_path: Path) -> dict[str, str]:
    if not profile_path.is_file():
        return {}
    try:
        data = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    contact = data.get("contact") or {}
    return {key: str(value or "").strip() for key, value in contact.items()}


class DocumentGenerator:
    def __init__(
        self,
        settings: Any,  # TailoringSettings
        project_root: Path,
        client: LLMClient,
        master_resume: str | None = None,
        extra_evidence: list[str] | None = None,
        contact: dict[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        today: date | None = None,
    ) -> None:
        self.settings = settings
        self.project_root = project_root
        self.client = client
        self.master_resume = master_resume or load_master_resume(
            self._path(settings.master_resume_path)
        )
        self.extra_evidence = extra_evidence or []
        self.contact = (
            contact
            if contact is not None
            else _load_contact(project_root / "config" / "application_profile.yaml")
        )
        self.sleep = sleep
        self.today = today or date.today()

    def _path(self, value: str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else self.project_root / candidate

    @property
    def output_directory(self) -> Path:
        return self._path(self.settings.output_directory)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def select_jobs(
        self,
        jobs: Iterable[dict[str, Any]],
        done_urls: set[str],
        result: GenerationResult,
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for job in jobs:
            url = str(job.get("job_url") or "")
            if not url or url in seen or job.get("status") in CLOSED_STATUSES:
                continue
            seen.add(url)
            score = job.get("match_score")
            if score is None or float(score) < self.settings.min_match_score:
                result.below_threshold += 1
                continue
            if url in done_urls:
                result.already_done += 1
                continue
            description = str(job.get("description") or "")
            if len(description) < self.settings.min_description_chars:
                result.no_description += 1
                continue
            selected.append(job)

        # Best matches first, so a quota cut-off loses the weakest jobs.
        selected.sort(key=lambda job: float(job.get("match_score") or 0), reverse=True)
        limit = self.settings.max_jobs_per_run
        if limit and len(selected) > limit:
            result.deferred += len(selected) - limit
            selected = selected[:limit]
        return selected

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def generate(self, database: Database, jobs: Iterable[dict[str, Any]]) -> GenerationResult:
        result = GenerationResult()
        done = database.get_tailored_documents()
        # A record whose files were deleted is regenerated.
        done_urls = {
            url for url, record in done.items()
            if Path(record["resume_path"]).is_file() and Path(record["cover_letter_path"]).is_file()
        }
        selected = self.select_jobs(jobs, done_urls, result)
        if not selected:
            return result

        logger.info("Tailoring documents for %s job(s) scoring >= %s",
                    len(selected), self.settings.min_match_score)
        consecutive_failures = 0
        for index, job in enumerate(selected):
            if index:
                self.sleep(self.settings.seconds_between_requests)
            url = job["job_url"]
            try:
                record = self.generate_one(job)
            except (LLMError, OSError) as error:
                result.failed.append((url, str(error)))
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    remaining = len(selected) - index - 1
                    logger.warning("Stopping tailoring after 3 failures in a row "
                                   "(quota or network?); %s job(s) left for next run.", remaining)
                    result.deferred += remaining
                    break
                continue
            consecutive_failures = 0
            database.save_tailored_documents(**record)
            result.created.append({**job, **record})
            logger.info("Tailored: %s | %s -> %s", job.get("title"), job.get("company"),
                        Path(record["resume_path"]).parent)
        return result

    def generate_one(self, job: dict[str, Any]) -> dict[str, Any]:
        application = tailor_application(job, self.master_resume, self.client, self.extra_evidence)
        self._apply_contact(application)

        company = str(job.get("company") or "")
        title = str(job.get("title") or "")
        folder = self.output_directory / (
            f"{self.today.isoformat()}_{slug(company, 30)}_{slug(title, 40)}"
        )
        person = slug(application.candidate.name or "Candidate", 30)
        resume_path = write_resume(
            application, folder / f"{person}_Resume_{slug(company, 30)}.docx", title
        )
        letter_path = write_cover_letter(
            application, folder / f"{person}_Cover_Letter_{slug(company, 30)}.docx", company, title,
            today=self.today,
        )
        report_path = self._write_report(job, application, folder / "match_report.md")
        return {
            "job_url": job["job_url"],
            "resume_path": str(resume_path.resolve()),
            "cover_letter_path": str(letter_path.resolve()),
            "report_path": str(report_path.resolve()),
            "keyword_coverage": application.keyword_coverage,
            "match_score": job.get("match_score"),
            "model": getattr(self.client, "model_name", ""),
        }

    def _apply_contact(self, application: TailoredApplication) -> None:
        """Prefer contact details from application_profile.yaml when filled in."""
        if self.contact.get("full_name"):
            application.candidate.name = self.contact["full_name"]
        parts = [
            self.contact.get(key, "")
            for key in ("email", "phone", "current_location", "linkedin")
        ]
        parts = [part for part in parts if part]
        if len(parts) >= 2:
            application.candidate.contact_line = " | ".join(parts)

    @staticmethod
    def _write_report(job: dict[str, Any], application: TailoredApplication, path: Path) -> Path:
        def bullet_list(items: list[str]) -> str:
            return "\n".join(f"- {item}" for item in items) or "- (none)"

        text = f"""# {job.get('title')} - {job.get('company')}

- Job link: {job.get('job_url')}
- Match score: {job.get('match_score')} ({job.get('match_category', '')})
- ATS keyword coverage of tailored resume: {application.keyword_coverage}%

## Job keywords found in your tailored resume
{bullet_list(application.matched_keywords)}

## Job keywords NOT in your tailored resume
{bullet_list(application.unmatched_keywords)}

## Requirements your master resume does not show
These were deliberately left out. If you do have this experience, add it to
your master resume and regenerate.
{bullet_list(application.missing_keywords)}

## Fact-check removals
Content the model produced that could not be verified against your master
resume and was removed:
{bullet_list(application.fact_check_warnings)}

Review both documents before uploading.
"""
        path.write_text(text, encoding="utf-8")
        return path
