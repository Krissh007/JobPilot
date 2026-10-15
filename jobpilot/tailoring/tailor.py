"""
Ask the model for a tailored resume + cover letter, then fact-check the answer
against the master resume so nothing invented reaches the documents.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field, ValidationError

from jobpilot.tailoring.llm import LLMClient, LLMError
from jobpilot.tailoring.prompts import SYSTEM_PROMPT, build_prompt

_STOPWORDS = {
    "and", "the", "for", "with", "of", "in", "on", "to", "a", "an", "&", "using",
    "tools", "tool", "skills", "management", "based", "via",
}


class _Loose(BaseModel):
    model_config = {"extra": "ignore"}


def _clean_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(item).strip() for item in value if str(item or "").strip()]


StrList = Annotated[list[str], BeforeValidator(_clean_str_list)]


class Candidate(_Loose):
    name: str = ""
    contact_line: str = ""


class ExperienceEntry(_Loose):
    title: str = ""
    company: str = ""
    location: str = ""
    dates: str = ""
    bullets: StrList = Field(default_factory=list)


class ProjectEntry(_Loose):
    name: str = ""
    bullets: StrList = Field(default_factory=list)


class EducationEntry(_Loose):
    degree: str = ""
    institution: str = ""
    dates: str = ""
    details: str = ""


class CoverLetter(_Loose):
    salutation: str = "Dear Hiring Manager,"
    paragraphs: StrList = Field(default_factory=list)
    closing: str = "Sincerely,"


class TailoredApplication(_Loose):
    candidate: Candidate = Field(default_factory=Candidate)
    headline: str = ""
    summary: str = ""
    skills: StrList = Field(default_factory=list)
    experience: list[ExperienceEntry] = Field(default_factory=list)
    projects: list[ProjectEntry] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    certifications: StrList = Field(default_factory=list)
    cover_letter: CoverLetter = Field(default_factory=CoverLetter)
    jd_keywords: StrList = Field(default_factory=list)
    missing_keywords: StrList = Field(default_factory=list)

    # Filled in locally, not by the model.
    matched_keywords: list[str] = Field(default_factory=list)
    unmatched_keywords: list[str] = Field(default_factory=list)
    keyword_coverage: float = 0.0
    fact_check_warnings: list[str] = Field(default_factory=list)

    def resume_text(self) -> str:
        parts = [self.headline, self.summary, *self.skills, *self.certifications]
        for entry in self.experience:
            parts += [entry.title, entry.company, *entry.bullets]
        for project in self.projects:
            parts += [project.name, *project.bullets]
        for school in self.education:
            parts += [school.degree, school.institution, school.details]
        return "\n".join(parts)


# ----------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#.%]+", " ", text.lower()).split())


def _tokens(text: str) -> list[str]:
    return [
        token.strip(".")
        for token in normalize(text).split()
        if len(token.strip(".")) > 1 and token.strip(".") not in _STOPWORDS
    ]


def is_supported(phrase: str, source_text: str, source_tokens: set[str]) -> bool:
    """True when a phrase (skill, company, ...) is evidenced by the master resume."""
    normalized = normalize(phrase)
    if not normalized:
        return False
    if normalized in source_text:
        return True
    tokens = _tokens(phrase)
    return bool(tokens) and all(
        token in source_tokens or token.rstrip("s") in source_tokens for token in tokens
    )


_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> set[str]:
    return {match.replace(",", "") for match in _NUMBER.findall(text)}


def keyword_present(keyword: str, text: str) -> bool:
    normalized_keyword = normalize(keyword)
    normalized_text = normalize(text)
    if normalized_keyword and normalized_keyword in normalized_text:
        return True
    tokens = _tokens(keyword)
    text_tokens = set(_tokens(text))
    return bool(tokens) and all(token in text_tokens for token in tokens)


# ----------------------------------------------------------------------
# Fact check
# ----------------------------------------------------------------------


def fact_check(
    application: TailoredApplication,
    master_resume: str,
    extra_evidence: list[str] | None = None,
) -> TailoredApplication:
    """Remove anything the master resume does not support and record why."""
    source_text = normalize(master_resume)
    source_tokens = set(_tokens(master_resume))
    evidence_text = normalize(" ".join(extra_evidence or []))
    source_numbers = _numbers(master_resume)
    warnings: list[str] = []

    def supported(phrase: str) -> bool:
        return is_supported(phrase, source_text, source_tokens) or (
            bool(evidence_text) and normalize(phrase) in evidence_text
        )

    def check_numbers(text: str, where: str) -> bool:
        invented = _numbers(text) - source_numbers
        if invented:
            warnings.append(f"Removed {where} with numbers not in master resume ({', '.join(sorted(invented))}): {text}")
            return False
        return True

    kept_skills = []
    for skill in application.skills:
        if supported(skill):
            kept_skills.append(skill)
        else:
            warnings.append(f"Removed unsupported skill: {skill}")
    application.skills = list(dict.fromkeys(kept_skills))

    kept_experience = []
    for entry in application.experience:
        if entry.company and not supported(entry.company):
            warnings.append(f"Removed role at unknown company: {entry.title} @ {entry.company}")
            continue
        entry.bullets = [b for b in entry.bullets if check_numbers(b, "bullet")]
        kept_experience.append(entry)
    application.experience = kept_experience

    for project in application.projects:
        project.bullets = [b for b in project.bullets if check_numbers(b, "project bullet")]
    application.projects = [p for p in application.projects if p.name or p.bullets]

    kept_education = []
    for school in application.education:
        if school.institution and not supported(school.institution):
            warnings.append(f"Removed unknown institution: {school.institution}")
            continue
        kept_education.append(school)
    application.education = kept_education

    kept_certs = []
    for certification in application.certifications:
        if supported(certification):
            kept_certs.append(certification)
        else:
            warnings.append(f"Removed unsupported certification: {certification}")
    application.certifications = kept_certs

    if not check_numbers(application.summary, "summary"):
        # Keep the summary but strip the offending sentence(s).
        sentences = re.split(r"(?<=[.!?])\s+", application.summary)
        application.summary = " ".join(
            s for s in sentences if not (_numbers(s) - source_numbers)
        )

    application.cover_letter.paragraphs = [
        p for p in application.cover_letter.paragraphs if check_numbers(p, "cover-letter paragraph")
    ]

    application.fact_check_warnings = warnings
    return application


def score_keywords(application: TailoredApplication) -> TailoredApplication:
    text = application.resume_text()
    keywords = list(dict.fromkeys(application.jd_keywords))
    matched = [k for k in keywords if keyword_present(k, text)]
    application.matched_keywords = matched
    application.unmatched_keywords = [k for k in keywords if k not in matched]
    application.keyword_coverage = round(100 * len(matched) / len(keywords)) if keywords else 0
    return application


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


def tailor_application(
    job: dict[str, Any],
    master_resume: str,
    client: LLMClient,
    extra_evidence: list[str] | None = None,
) -> TailoredApplication:
    raw = client.generate_json(SYSTEM_PROMPT, build_prompt(job, master_resume))
    try:
        application = TailoredApplication.model_validate(raw)
    except ValidationError as error:
        raise LLMError(f"Model output did not match the expected structure: {error}") from error

    application = fact_check(application, master_resume, extra_evidence)
    if not application.experience:
        raise LLMError("No verifiable experience left after fact check")
    if len(application.cover_letter.paragraphs) < 2:
        raise LLMError("Cover letter too short after fact check")
    return score_keywords(application)
