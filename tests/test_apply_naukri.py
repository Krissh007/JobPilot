"""
Naukri apply against a FAKE Naukri posting (Playwright routing, headless installed Chrome),
built with the legacy module's own selectors. Real legacy NaukriApplier + JobPilot subclass + runner.
The page counts Apply clicks: on Naukri, Apply IS the submit, so it must stay 0 until you approve.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlmodel import Session, select

from jobpilot.agents.apply import runner
from jobpilot.db.models import Actor, Answer, Application, ApplicationState, FieldSource, JobStatus, StatusHistory
from jobpilot.db.repo import put_setting, upsert_job
from jobpilot.db.session import get_engine
from jobpilot.resume import Resume
from tests.test_apply import FakePacing, chrome, set_auto, state  # noqa: F401  (chrome is a fixture)

pytest.importorskip("playwright")

JOB_URL = "https://www.naukri.com/job-listings-ai-business-analyst-acme-hyderabad-2-to-5-years-150926599999"

PAGE = """<!doctype html><html><body><main><h1>AI Business Analyst</h1>__BUTTONS__
<div id="chat"></div></main>
<script>
window.__applies = 0;
const questions = __QUESTIONS__;
let q = 0;
function done() { document.getElementById('chat').innerHTML = '<p>You have successfully applied to AI Business Analyst</p>'; }
function thanks() {  // like the real Naukri: a closing bot line with nothing to answer, then the confirmation
  document.getElementById('chat').innerHTML = '<div class="chatbot_Drawer"><div class="botMsg">Thank you for your responses.</div></div>';
  setTimeout(done, 2500);
}
function ask() {
  if (q >= questions.length) return questions.length ? thanks() : done();
  document.getElementById('chat').innerHTML =
    '<div class="chatbot_Drawer"><div class="botMsg">' + questions[q] + '</div>' +
    '<textarea id="reply"></textarea><button class="sendMsg">Save</button></div>';
  document.querySelector('.sendMsg').onclick = () => {
    if (!document.getElementById('reply').value) return;
    window.__answers = (window.__answers || []).concat([document.getElementById('reply').value]);
    q++; ask();
  };
}
const b = document.getElementById('apply-button');
if (b) b.onclick = () => { window.__applies++; b.remove(); ask(); };
</script></body></html>"""

APPLY = '<button id="apply-button">Apply</button>'
FOLLOW = ('<button id="apply-button">Apply</button><div><input type="checkbox" id="fl" checked>'
          '<label for="fl">Follow Acme as you apply to stay updated</label></div>')
COMPANY_SITE = '<button id="company-site-button">Apply on company site</button>'
LOGIN = '<p>Login to apply</p><button id="apply-button">Apply</button>'


def factory(chrome, buttons=APPLY, questions=(), signed_in=True):
    holder = {}

    @contextmanager
    def make():
        context = chrome.new_context()
        if signed_in:
            context.add_cookies([{"name": "nauk_at", "value": "test", "domain": ".naukri.com", "path": "/"}])
        html = PAGE.replace("__BUTTONS__", buttons).replace("__QUESTIONS__", repr(list(questions)))
        context.route("https://www.naukri.com/**", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        holder["context"] = context
        try:
            yield context
        finally:
            context.close()

    return make, holder


def page_value(holder, expr):
    pages = holder["context"].pages
    return pages[0].evaluate(expr) if pages else None


@pytest.fixture
def naukri_job(db_url, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "SCREENSHOTS", tmp_path / "shots")
    monkeypatch.setattr(runner, "load_resume", lambda: Resume(path=tmp_path / "r.md", text="KRISHNA\nBusiness Analyst " * 30))
    monkeypatch.setattr(runner, "load_contact", lambda: {"full_name": "Krishna Test", "phone": "9000000000", "email": "k@example.com"})
    monkeypatch.setattr(runner, "notify", lambda *a, **k: True)
    with Session(get_engine(db_url)) as s:
        job, _ = upsert_job(s, {"title": "AI Business Analyst", "company": "Acme", "source": "naukri", "url": JOB_URL,
                                "jd_text": "LLM requirements " * 30, "score": 82, "status": "docs_ready"})
        resume_file, letter_file = tmp_path / "Resume.docx", tmp_path / "Cover.docx"
        resume_file.write_bytes(b"x")
        letter_file.write_bytes(b"x")
        job.resume_path, job.cover_letter_path = str(resume_file), str(letter_file)   # documents are on disk
        s.add(job)
        s.add(Answer(question="notice period", answer="30 days", tags="t"))
        put_setting(s, "review_wait_minutes", 0.1)  # a broken test fails in seconds, not 20 min
        s.commit()
        return job.id


def run(db_url, make, pacing):
    runner.run(db_url=db_url, browser_factory=make, pacing=pacing, drafter_factory=lambda job: None)


def test_nothing_is_sent_until_you_approve(db_url, naukri_job, chrome):
    set_auto(db_url, False)
    make, holder = factory(chrome, questions=["What is your notice period?"])
    seen = {}

    class You(FakePacing):
        def _sleep(self, _seconds):
            with Session(get_engine(db_url)) as s:
                app = s.exec(select(Application).where(Application.state == ApplicationState.pending_review)).first()
                if app and "applies" not in seen:
                    seen["applies"] = page_value(holder, "window.__applies")  # while waiting for you
                    app.state = ApplicationState.approved
                    s.add(app)
                    s.commit()

    run(db_url, make, You())
    job, app, fields, rec = state(db_url, naukri_job)
    assert seen["applies"] == 0
    assert app.state == ApplicationState.submitted and job.status == JobStatus.applied and app.screenshot_path
    assert {f.field_name: (f.value_used, f.source) for f in fields}["What is your notice period?"] == ("30 days", FieldSource.answer_bank)
    assert "Nothing sent yet" in rec.log and "APPLIED on Naukri (after your approval" in rec.log
    with Session(get_engine(db_url)) as s:
        assert s.exec(select(StatusHistory).where(StatusHistory.job_id == naukri_job)).all()[-1].actor == Actor.user


def test_auto_submit_when_every_chat_answer_is_in_the_bank(db_url, naukri_job, chrome):
    set_auto(db_url, True)
    make, holder = factory(chrome, questions=["What is your notice period?"])
    run(db_url, make, FakePacing())
    job, app, _, rec = state(db_url, naukri_job)
    assert app.state == ApplicationState.submitted and job.status == JobStatus.applied
    assert "APPLIED on Naukri (auto-submit; 1 chatbot answer(s)" in rec.log and "waiting" not in rec.log.lower()


def hold_window_open(db_url):
    with Session(get_engine(db_url)) as s:
        put_setting(s, "park_incomplete", False)
        s.commit()


def test_unknown_chat_question_is_left_to_you_and_recorded(db_url, naukri_job, chrome):
    set_auto(db_url, True)
    hold_window_open(db_url)
    make, holder = factory(chrome, questions=["What is your notice period?", "Rate your Power BI skills out of 10"])

    class You(FakePacing):
        def _sleep(self, _seconds):  # you type the answer in the agent's window
            page = holder["context"].pages[0]
            if page.locator("#reply").count():
                page.fill("#reply", "7")
                page.click(".sendMsg")

    run(db_url, make, You())
    job, app, fields, rec = state(db_url, naukri_job)
    assert "Naukri needs you: Rate your Power BI skills out of 10" in rec.log and "you finished it in the browser" in rec.log
    assert app.state == ApplicationState.submitted and job.status == JobStatus.applied
    assert any(f.field_name.startswith("Rate your Power BI") and f.value_used == "(empty: needs you)" for f in fields)
    with Session(get_engine(db_url)) as s:
        open_q = s.exec(select(Answer).where(Answer.question == "Rate your Power BI skills out of 10")).one()
    assert open_q.answer == "" and "needs-answer" in open_q.tags


def test_consent_question_is_never_answered(db_url, naukri_job, chrome):
    set_auto(db_url, True)
    hold_window_open(db_url)
    with Session(get_engine(db_url)) as s:
        s.add(Answer(question="I agree to the terms and conditions", answer="Yes", tags="t"))
        s.commit()
    make, holder = factory(chrome, questions=["I agree to the terms and conditions of Acme"])
    seen = {}

    class Watch(FakePacing):
        def _sleep(self, _seconds):
            seen["answers"] = page_value(holder, "window.__answers")
            with Session(get_engine(db_url)) as s:
                app = s.exec(select(Application).where(Application.state == ApplicationState.pending_review)).first()
                if app:
                    app.state = ApplicationState.skipped
                    s.add(app)
                    s.commit()

    run(db_url, make, Watch())
    _, app, fields, rec = state(db_url, naukri_job)
    assert seen["answers"] is None  # nothing typed into the consent question
    assert app.reason == "skipped_by_you" and any(f.value_used.startswith("(not ticked") for f in fields)


def test_company_site_job_is_external_and_never_retried(db_url, naukri_job, chrome):
    make, holder = factory(chrome, buttons=COMPANY_SITE)
    run(db_url, make, FakePacing())
    job, app, _, rec = state(db_url, naukri_job)
    assert app.reason == "external" and job.status == JobStatus.docs_ready and "company's site" in rec.log
    run(db_url, make, FakePacing())
    _, _, _, again = state(db_url, naukri_job)
    assert "Nothing to do" in again.log


def test_login_prompt_blocks_without_clicking(db_url, naukri_job, chrome, monkeypatch):
    monkeypatch.setattr(runner.time, "monotonic", iter(range(0, 10**9, 10**6)).__next__)  # skip the 15-min hold
    make, holder = factory(chrome, buttons=LOGIN)
    with pytest.raises(runner.Blocked):
        run(db_url, make, FakePacing())
    _, app, _, rec = state(db_url, naukri_job)
    assert app.state == ApplicationState.blocked and "BLOCKED" in rec.log


def test_signed_out_portal_is_skipped_with_the_login_hint(db_url, naukri_job, chrome):
    make, _ = factory(chrome, signed_in=False)
    with pytest.raises(RuntimeError):
        run(db_url, make, FakePacing())
    _, app, _, rec = state(db_url, naukri_job)
    assert app is None and "python -m jobpilot.agents.apply.login naukri" in rec.log


def test_closing_message_is_not_a_question_and_follow_is_unticked(db_url, naukri_job, chrome):
    set_auto(db_url, True)
    make, holder = factory(chrome, buttons=FOLLOW, questions=["What is your notice period?"])
    seen = {}
    original = runner.mark_submitted

    def spy(session, app, job, by):
        seen["follow_checked"] = holder["context"].pages[0].evaluate("document.getElementById('fl') && document.getElementById('fl').checked")
        return original(session, app, job, by)

    runner.mark_submitted = spy
    try:
        run(db_url, make, FakePacing())
    finally:
        runner.mark_submitted = original
    job, app, fields, rec = state(db_url, naukri_job)
    assert app.state == ApplicationState.submitted and app.reason == "" and "needs you" not in rec.log
    assert seen["follow_checked"] is False
    assert {f.field_name: f.value_used for f in fields}["Follow Acme as you apply"] == "unticked by the agent"
    with Session(get_engine(db_url)) as s:
        assert not s.exec(select(Answer).where(Answer.question.contains("Thank you"))).all()



def test_unknown_chat_question_is_parked_with_the_answered_ones_recorded(db_url, naukri_job, chrome):
    set_auto(db_url, True)   # Naukri's Apply button IS the submit, so the chat only starts with auto-submit or an approval
    make, holder = factory(chrome, questions=["What is your notice period?", "Rate your Power BI skills out of 10"])
    run(db_url, make, FakePacing())
    job, app, fields, rec = state(db_url, naukri_job)
    assert app.state == ApplicationState.pending_review and app.reason == "draft_unsaved"
    assert all(p.is_closed() for p in holder["context"].pages)                      # moved on, window not held open
    by_name = {f.field_name: f for f in fields}
    assert by_name["What is your notice period?"].value_used == "30 days"            # answered from the bank
    blank = by_name["Rate your Power BI skills out of 10"]
    assert blank.value_used == "(empty: needs you)" and blank.needs_review
    assert job.status == JobStatus.docs_ready and "PARKED" in rec.log and "Naukri keeps the chat" in rec.log
    with Session(get_engine(db_url)) as s:
        assert runner.select_jobs(s, None, 60) == []                                # not opened again until you decide


def test_consent_question_is_parked_and_never_answered(db_url, naukri_job, chrome):
    set_auto(db_url, True)
    with Session(get_engine(db_url)) as s:
        s.add(Answer(question="I agree to the terms and conditions", answer="Yes", tags="t"))
        s.commit()
    make, holder = factory(chrome, questions=["I agree to the terms and conditions of Acme"])
    run(db_url, make, FakePacing())
    _, app, fields, _ = state(db_url, naukri_job)
    assert app.reason == "draft_unsaved" and any(f.value_used.startswith("(not ticked") for f in fields)
