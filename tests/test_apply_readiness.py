from __future__ import annotations

from itertools import count

import pytest
from sqlmodel import Session

from jobpilot.agents.apply import readiness
from jobpilot.db.models import (Actor, Application, ApplicationField, ApplicationState, FieldSource, JobStatus)
from jobpilot.db.repo import set_status, upsert_job

_n = count(1)


def make(session: Session, state, reason="", sources=(FieldSource.resume,), review=False, applied_by=None,
         is_example=False, extra_fields=()) -> int:
    n = next(_n)
    job, _ = upsert_job(session, {"title": "BA", "company": f"Co{n}", "url": f"https://www.linkedin.com/jobs/view/44000000{n:02d}"})
    app = Application(job_id=job.id, state=state, reason=reason, is_example=is_example)
    session.add(app)
    session.flush()
    for src in sources:
        session.add(ApplicationField(application_id=app.id, field_name="f", source=src, needs_review=review))
    for name in extra_fields:   # empty / consent rows the apply agent records with needs_review
        session.add(ApplicationField(application_id=app.id, field_name=name, source=FieldSource.answer_bank, needs_review=True))
    if applied_by:
        set_status(session, job, JobStatus.applied, actor=applied_by)
    session.commit()
    return app.id


def outcomes(session) -> dict[int, tuple[str, bool]]:
    return {c.app_id: (c.outcome, c.clean) for c in readiness.compute(session).cases}


def test_classification_of_every_kind_of_application(session):
    approved_clean = make(session, ApplicationState.submitted, applied_by=Actor.user, sources=(FieldSource.resume, FieldSource.answer_bank))
    approved_drafted = make(session, ApplicationState.submitted, applied_by=Actor.user, sources=(FieldSource.llm,), review=True)
    skipped_clean = make(session, ApplicationState.skipped, "skipped_by_you")
    skipped_consent = make(session, ApplicationState.skipped, "skipped_by_you", extra_fields=["Terms"])
    timeout = make(session, ApplicationState.skipped, "timeout")
    external = make(session, ApplicationState.skipped, "external", sources=())
    failed = make(session, ApplicationState.skipped, "failed", sources=())
    blocked = make(session, ApplicationState.blocked, "blocked", sources=())
    waiting = make(session, ApplicationState.pending_review)
    incomplete = make(session, ApplicationState.pending_review, "incomplete")
    auto = make(session, ApplicationState.submitted, applied_by=Actor.agent)
    already = make(session, ApplicationState.submitted, applied_by=Actor.agent, sources=())
    example = make(session, ApplicationState.submitted, applied_by=Actor.user, is_example=True)

    got = outcomes(session)
    assert got[approved_clean] == ("approved", True)
    assert got[approved_drafted] == ("approved", False)       # a Gemini draft is never clean
    assert got[skipped_clean] == ("skipped", True)
    assert got[skipped_consent] == ("skipped", False)         # consent box left for you
    assert got[timeout][0] == "undecided"
    assert got[external][0] == got[failed][0] == got[blocked][0] == "not_a_review"
    assert got[waiting] == ("pending", True)
    assert got[incomplete] == ("pending", False)              # stuck before Submit
    assert got[auto] == ("auto_submitted", True)
    assert got[already][0] == "not_a_review"                  # "you already applied", no fields
    assert example not in got                                 # example data never counts


def review(session, approved: int, skipped: int, clean=True):
    for _ in range(approved):
        make(session, ApplicationState.submitted, applied_by=Actor.user, extra_fields=[] if clean else ["Empty question"])
    for _ in range(skipped):
        make(session, ApplicationState.skipped, "skipped_by_you", extra_fields=[] if clean else ["Empty question"])


def test_not_ready_until_enough_clean_reviews(session):
    review(session, 19, 0)
    r = readiness.compute(session)
    assert not r.ready and "NOT YET: 19/20" in r.verdict()
    review(session, 1, 0)
    assert readiness.compute(session).ready


def test_not_ready_when_clean_approval_rate_too_low(session):
    review(session, 18, 2)     # 90%
    r = readiness.compute(session)
    assert not r.ready and "90%" in r.verdict() and "below 95%" in r.verdict()
    review(session, 10, 0)     # 28/30 = 93%
    assert not readiness.compute(session).ready
    review(session, 40, 0)     # 68/70 = 97%
    assert readiness.compute(session).ready


def test_reviews_that_needed_attention_do_not_count_toward_readiness(session):
    review(session, 30, 0, clean=False)
    r = readiness.compute(session)
    assert r.decided() == 30 and r.decided(True) == 0 and not r.ready
    assert r.approval_rate(True) is None


def test_thresholds_are_configurable(session):
    review(session, 5, 0)
    assert readiness.compute(session, min_decisions=5, min_approval=1.0).ready


def test_report_text_and_empty_database(session, capsys):
    assert "NOT YET: 0/20" in readiness.compute(session).report()
    review(session, 3, 1)
    text = readiness.compute(session).report()
    assert "Clean reviews" in text and "approved   3" in text and "skipped   1" in text and "75%" in text


def test_readiness_never_changes_the_auto_submit_setting(session):
    from jobpilot.db.repo import get_setting

    review(session, 25, 0)
    assert readiness.compute(session).ready
    assert get_setting(session, "auto_submit") is False
