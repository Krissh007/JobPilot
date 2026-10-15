"""
One-off import of screening answers from the legacy config/application_profile.yaml
into the answer bank. Idempotent: questions already in the bank are left alone.
Empty answers come in as open questions (answer "", tag "needs-answer"); the
apply agent never fills a form from them, and the weekly review lists them first.
Contact details (name, email, phone) are not answers; the apply agent reads them
from the profile directly.

    python -m jobpilot.agents.apply.answers_import
"""

from __future__ import annotations

from typing import Any

import yaml
from sqlmodel import Session, select

from jobpilot.db.models import Answer
from jobpilot.db.session import session_scope
from jobpilot.config import application_profile_path

# application_profile.yaml "application" keys -> the question wording forms use
APPLICATION_QUESTIONS = {
    "notice_period_days": "Notice period (days)",
    "expected_ctc_lpa": "Expected CTC (LPA)",
    "willing_to_relocate": "Willing to relocate",
    "work_authorization_india": "Authorized to work in India",
}
NEEDS_ANSWER = "needs-answer"


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("?", " ").split())


def collect(profile: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(question, answer, tags) from the profile, screening answers first."""
    rows: list[tuple[str, str, str]] = []
    for question, answer in (profile.get("screening_answers") or {}).items():
        rows.append((str(question).strip(), str(answer or "").strip(), "imported, screening"))
    for key, question in APPLICATION_QUESTIONS.items():
        rows.append((question, str((profile.get("application") or {}).get(key) or "").strip(), "imported, application"))
    return rows


def import_answers(session: Session, profile: dict[str, Any]) -> dict[str, int]:
    existing = {_norm(a.question) for a in session.exec(select(Answer))}
    counts = {"answers": 0, "open_questions": 0, "already_there": 0}
    for question, answer, tags in collect(profile):
        if _norm(question) in existing:
            counts["already_there"] += 1
            continue
        existing.add(_norm(question))
        if not answer:
            tags += f", {NEEDS_ANSWER}"
            counts["open_questions"] += 1
        else:
            counts["answers"] += 1
        session.add(Answer(question=question, answer=answer, tags=tags))
    return counts


def main() -> None:
    path = application_profile_path()
    profile = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    with session_scope() as session:
        counts = import_answers(session, profile)
    print(f"Imported from {path}: {counts}")


if __name__ == "__main__":
    main()
