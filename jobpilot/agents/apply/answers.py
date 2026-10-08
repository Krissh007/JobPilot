"""
Where every form value comes from, in order:
1. resume/profile: contact details from the legacy application_profile.yaml
2. answer_bank: answers you wrote (Answers screen); open questions are skipped
3. llm: a Gemini draft from your resume (+ the JD), only if the resume supports
   an answer; always needs_review, so such an application is never auto-submitted.
Questions with no bank answer are added to the bank as open questions for the
weekly review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

import yaml
from sqlmodel import Session, select

from jobpilot.db.models import Answer, FieldSource, Job, utcnow
from jobpilot.legacy import bridge
from jobpilot.llm.cache import cache_get, cache_key, cache_put
from jobpilot.resume import Resume

NEEDS_ANSWER = "needs-answer"


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text).lower()).split())


@dataclass
class Resolved:
    value: str
    source: FieldSource
    needs_review: bool = False


class AnswerBank:
    def __init__(self, session: Session, contact: dict[str, str]) -> None:
        self.session = session
        self.profile: dict[str, str] = {}
        name = contact.get("full_name", "").strip()
        first, _, last = name.partition(" ")
        for key, value in {"first name": first, "last name": last, "full name": name, "email": contact.get("email", ""),
                           "phone": contact.get("phone", ""), "mobile": contact.get("phone", ""),
                           "city": contact.get("current_location", ""), "location": contact.get("current_location", ""),
                           "linkedin": contact.get("linkedin", "")}.items():
            if value:
                self.profile[key] = value
        self.answers: dict[str, Answer] = {}
        self.open: set[str] = set()
        for answer in session.exec(select(Answer).where(Answer.is_example.is_(False))):
            key = norm(answer.question)
            if answer.answer.strip():
                self.answers[key] = answer
            else:
                self.open.add(key)

    def lookup(self, label: str) -> Resolved | None:
        text = norm(label)
        if not text:
            return None
        # Your written answers win over profile fields; the longest matching phrase wins
        # ("notice period in days" beats "notice period").
        hits = [k for k in self.answers if k and (k in text or text in k)]
        if hits:
            answer = self.answers[max(hits, key=len)]
            answer.times_used += 1
            self.session.add(answer)
            return Resolved(answer.answer.strip(), FieldSource.answer_bank)
        hits = [k for k in self.profile if k in text]
        if hits:
            return Resolved(self.profile[max(hits, key=len)], FieldSource.resume)
        return None

    def note_unknown(self, label: str) -> None:
        """A question the bank can't answer becomes an open question for the weekly review."""
        key = norm(label)
        if not key or key in self.open or key in self.answers:
            return
        self.open.add(key)
        self.session.add(Answer(question=label.strip(), answer="", tags=f"{NEEDS_ANSWER}, seen in form",
                                created_at=utcnow(), updated_at=utcnow()))


DRAFT_SYSTEM = """You fill in one job-application form field for a candidate.
Use ONLY facts in the candidate's resume (and the job description for context).
Never invent experience, numbers, certifications, salary or personal circumstances.
If the resume does not clearly support an answer (e.g. expected salary, notice period,
visa status, willingness to relocate, years with a tool the resume doesn't show), return null.
Numeric questions: answer with a number only. If options are given, answer with exactly
one option's text. Free-text answers: at most 2 sentences, first person, plain text.
Return JSON only: {"answer": "<text>"} or {"answer": null}."""


def make_drafter(client_factory: Callable[[], Any], session: Session, resume: Resume, job: Job,
                 log: Callable[[str], None]) -> Callable[[str, list[str] | None], str | None]:
    """Gemini drafter with the sha256 cache; returns None when the resume can't support an answer."""
    state: dict[str, Any] = {}

    def draft(question: str, options: list[str] | None = None) -> str | None:
        options = [o for o in (options or []) if o.strip()]
        prompt = (f"FORM QUESTION: {question}\n"
                  + (f"OPTIONS: {' | '.join(options)}\n" if options else "")
                  + f"\nJOB: {job.title} at {job.company}\n{job.jd_text[:4000]}\n\nCANDIDATE RESUME\n{resume.text}")
        key = cache_key("answer:v1", f"{question}|{'|'.join(options)}|{job.jd_text[:4000]}", resume.version)
        data = cache_get(session, key)
        if data is None:
            if "client" not in state:
                state["client"] = client_factory()
            try:
                data = state["client"].generate_json(DRAFT_SYSTEM, prompt)
            except Exception as error:  # a failed draft just leaves the field for you
                log(f"      Gemini draft failed for '{question[:60]}': {str(error)[:120]}")
                return None
            cache_put(session, key, "answer:v1", data)
        answer = data.get("answer") if isinstance(data, dict) else None
        if answer is None or not str(answer).strip():
            return None
        answer = str(answer).strip()
        if options and answer.lower() not in {o.lower() for o in options}:
            return None  # must be one of the offered options
        return answer

    return draft


def load_contact() -> dict[str, str]:
    path = bridge.application_profile_path()
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {k: str(v or "").strip() for k, v in (data.get("contact") or {}).items()}
