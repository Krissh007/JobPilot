"""
Weekly review of the answer bank. Due every week at the configured day/time (IST,
default Monday 09:00) until you press "Mark reviewed" in the dashboard.

    python -m jobpilot.weekly          # notify if due (Phase 8 schedules this Mondays 09:00)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, select

from jobpilot.api.deps import IST
from jobpilot.db.models import Answer, utcnow
from jobpilot.db.repo import get_setting, put_setting
from jobpilot.db.session import session_scope

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def last_due(now: datetime, day: str, at: str) -> datetime:
    """Most recent day@time (IST) at or before now."""
    local = now.astimezone(IST)
    hour, minute = (int(x) for x in at.split(":"))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    candidate -= timedelta(days=(local.weekday() - DAYS.index(day)) % 7)
    if candidate > local:
        candidate -= timedelta(days=7)
    return candidate


def _aware(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)


def status(session: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    rule = get_setting(session, "weekly_review")
    due_at = last_due(now, rule["day"], rule["time"])
    reviewed = _aware(get_setting(session, "answers_reviewed_at"))
    answers = list(session.exec(select(Answer).where(Answer.is_example.is_(False))))
    week_start = due_at - timedelta(days=7)
    return {
        "due": reviewed is None or reviewed < due_at,
        "due_at": due_at.isoformat(),
        "next_due_at": (due_at + timedelta(days=7)).isoformat(),
        "reviewed_at": reviewed.isoformat() if reviewed else None,
        "open_questions": [{"id": a.id, "question": a.question, "created_at": a.created_at} for a in answers if not a.answer.strip()],
        "new_this_week": [{"id": a.id, "question": a.question, "answered": bool(a.answer.strip())}
                          for a in answers if (_aware(a.created_at) or now) >= week_start],
        "total_answers": sum(1 for a in answers if a.answer.strip()),
    }


def mark_reviewed(session: Session) -> None:
    put_setting(session, "answers_reviewed_at", utcnow().isoformat())


def main() -> None:
    from jobpilot.notify import notify

    with session_scope() as session:
        info = status(session)
    if info["due"]:
        notify("JobPilot: weekly answer review", f"{len(info['open_questions'])} open question(s), "
               f"{len(info['new_this_week'])} new this week. Review them so more applications can auto-submit.")
    print("due" if info["due"] else "not due", "|", len(info["open_questions"]), "open questions")


if __name__ == "__main__":
    main()
