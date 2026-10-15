from sqlmodel import select

from jobpilot.db.models import Answer
from jobpilot.agents.apply.answers_import import import_answers

PROFILE = {
    "contact": {"full_name": "K", "email": "k@example.com"},
    "application": {"notice_period_days": "30", "expected_ctc_lpa": "", "willing_to_relocate": None},
    "screening_answers": {"notice period": "30 days", "years of experience": "", "Notice Period?": "dup"},
}


def test_import_is_idempotent_and_flags_open_questions(session):
    first = import_answers(session, PROFILE)
    session.commit()
    assert first == {"answers": 2, "open_questions": 4, "already_there": 1}  # "Notice Period?" == "notice period"
    rows = {a.question: a for a in session.exec(select(Answer))}
    assert rows["notice period"].answer == "30 days" and "needs-answer" not in rows["notice period"].tags
    assert rows["years of experience"].answer == "" and "needs-answer" in rows["years of experience"].tags
    assert not any("k@example.com" in a.answer for a in rows.values())  # contact details are not answers
    assert import_answers(session, PROFILE)["already_there"] == 7
