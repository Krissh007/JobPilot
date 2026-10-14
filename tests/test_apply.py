"""
Apply agent against a FAKE LinkedIn Easy Apply page (Playwright routing, headless
installed Chrome). Real legacy filler + JobPilot subclass + runner; no real site.
The fake page counts Submit clicks so we can prove nothing is submitted without permission.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlmodel import Session, select

from jobpilot.agents.apply import runner
from jobpilot.db.models import (
    AgentRun,
    Answer,
    Application,
    ApplicationField,
    ApplicationState,
    FieldSource,
    Job,
    JobStatus,
    RunStatus,
)
from jobpilot.db.repo import put_setting, upsert_job
from jobpilot.db.session import get_engine
from jobpilot.resume import Resume

pytest.importorskip("playwright")

JOB_URL = "https://www.linkedin.com/jobs/view/4300000001/"
CHECKPOINT_URL = "https://www.linkedin.com/checkpoint/challenge/xyz"

FAKE_PAGE = """<!doctype html><html><body><main>
<h1>AI Business Analyst</h1><button id="ea">Easy Apply</button>
<div id="dlg" role="dialog" style="display:none"></div>
<div id="box" role="alertdialog" style="display:none"><p>Save this application?</p>
  <button id="discard">Discard</button><button id="save">Save</button></div></main>
<script>
window.__submits = 0;
const steps = [
  `<div><label for="ph">Mobile phone number</label><input id="ph" required></div>
   <div><label for="np">Notice period in days</label><input id="np" required></div>
   __EXTRA__
   <button id="next">Next</button>`,
  `<fieldset><legend>Are you willing to relocate?</legend>
     <input type="radio" id="ry" name="r" value="Yes"><label for="ry">Yes</label>
     <input type="radio" id="rn" name="r" value="No"><label for="rn">No</label></fieldset>
   <button id="review">Review</button>`,
  `<p>Review your application</p><button id="submit">Submit application</button>`,
];
let i = 0;
const PROMPT = __PROMPT__;
const dlg = document.getElementById('dlg');
const box = document.getElementById('box');
document.addEventListener('click', e => {
  const id = e.target.id;
  if (id === 'dismiss') {
    const touched = [...dlg.querySelectorAll('input,textarea')].some(x => x.value);
    if (touched && PROMPT) box.style.display = 'block'; else dlg.style.display = 'none';
  }
  if (id === 'save' || id === 'discard') { fetch('/__' + id); box.style.display = 'none'; dlg.style.display = 'none'; }
});
function show() {
  dlg.innerHTML = '<button aria-label="Dismiss" id="dismiss">x</button>' + steps[i];
  const n = dlg.querySelector('#next, #review');
  if (n) n.onclick = () => {
    const empty = [...dlg.querySelectorAll('[required]')].filter(e => e.type === 'checkbox' ? !e.checked : !e.value);
    if (empty.length) { const a = document.createElement('div'); a.setAttribute('role','alert'); a.textContent = 'Please enter a valid answer'; empty[0].parentElement.appendChild(a); return; }
    i++; show();
  };
  const s = dlg.querySelector('#submit');
  if (s) s.onclick = () => { window.__submits++; dlg.innerHTML = '<p>Your application was sent to Acme</p>'; };
}
document.getElementById('ea').onclick = () => { dlg.style.display = 'block'; show(); };
</script></body></html>"""

WHY = '<div><label for="why">Why are you interested in this role?</label><textarea id="why" required></textarea></div>'
CONSENT_BOX = ('<fieldset><legend>Acme has my consent to collect, store, and process my data for employment.*</legend>'
               '<input type="checkbox" id="ok" required><label for="ok">Yes</label></fieldset>')


@pytest.fixture
def chrome():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as error:
            pytest.skip(f"installed Chrome not available: {error}")
        yield browser
        browser.close()


def factory(chrome, extra: str = "", signed_in: bool = True, redirect_to_checkpoint: bool = False, save_prompt: bool = True):
    holder = {"save": 0, "discard": 0}

    @contextmanager
    def make():
        context = chrome.new_context()
        if signed_in:
            context.add_cookies([{"name": "li_at", "value": "test", "domain": ".www.linkedin.com", "path": "/"}])
        html = FAKE_PAGE.replace("__EXTRA__", extra).replace("__PROMPT__", "true" if save_prompt else "false")

        def handle(route):
            for name in ("save", "discard"):
                if route.request.url.endswith(f"/__{name}"):
                    holder[name] += 1
                    return route.fulfill(status=200, body="")
            if redirect_to_checkpoint and "jobs/view" in route.request.url:
                route.fulfill(status=200, content_type="text/html", body=f"<script>location.href='{CHECKPOINT_URL}'</script>")
            else:
                route.fulfill(status=200, content_type="text/html", body=html)

        context.route("https://www.linkedin.com/**", handle)
        holder["context"] = context
        try:
            yield context
        finally:
            context.close()

    return make, holder


class FakePacing(runner.Pacing):
    """No real waiting; optionally plays the user's decision on the first poll."""

    def __init__(self, db_url=None, decide=None):
        super().__init__(low=0, high=0, sleep=self._sleep, poll_seconds=0)
        self.db_url, self.decide = db_url, decide

    def _sleep(self, _seconds):
        if self.decide:
            with Session(get_engine(self.db_url)) as s:
                app = s.exec(select(Application).where(Application.state == ApplicationState.pending_review)).first()
                if app:
                    app.state = self.decide
                    s.add(app)
                    s.commit()


@pytest.fixture
def world(db_url, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "SCREENSHOTS", tmp_path / "shots")
    monkeypatch.setattr(runner, "load_resume", lambda: Resume(path=tmp_path / "r.md", text="KRISHNA\nBusiness Analyst " * 30))
    monkeypatch.setattr(runner, "load_contact", lambda: {"full_name": "Krishna Test", "phone": "9000000000", "email": "k@example.com"})
    monkeypatch.setattr(runner, "notify", lambda *a, **k: True)
    with Session(get_engine(db_url)) as s:
        job, _ = upsert_job(s, {"title": "AI Business Analyst", "company": "Acme", "source": "linkedin", "url": JOB_URL,
                                "jd_text": "LLM requirements " * 30, "score": 82, "status": "docs_ready"})
        s.add(Answer(question="Notice period in days", answer="30", tags="t"))
        s.add(Answer(question="willing to relocate", answer="Yes", tags="t"))
        s.commit()
        return job.id


def set_auto(db_url, on: bool):
    with Session(get_engine(db_url)) as s:
        put_setting(s, "auto_submit", on)
        s.commit()


def state(db_url, job_id):
    with Session(get_engine(db_url)) as s:
        app = s.exec(select(Application).where(Application.job_id == job_id).order_by(Application.id.desc())).first()
        fields = s.exec(select(ApplicationField).where(ApplicationField.application_id == app.id)).all() if app else []
        return s.get(Job, job_id), app, fields, s.exec(select(AgentRun).order_by(AgentRun.id.desc())).first()


def submits(holder) -> int:
    return holder["context"].pages[0].evaluate("window.__submits") if holder["context"].pages else -1


def test_auto_submits_only_when_every_answer_is_yours(db_url, world, chrome):
    set_auto(db_url, True)
    make, _ = factory(chrome)
    runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(), drafter_factory=lambda job: None)
    job, app, fields, run = state(db_url, world)
    assert app.state == ApplicationState.submitted and job.status == JobStatus.applied
    sources = {f.field_name: (f.value_used, f.source) for f in fields}
    assert sources["Mobile phone number"] == ("9000000000", FieldSource.resume)
    assert sources["Notice period in days"] == ("30", FieldSource.answer_bank)
    assert sources["Are you willing to relocate?"] == ("Yes", FieldSource.answer_bank)
    assert app.screenshot_path and run.status == RunStatus.ok and run.found == 1
    assert "AUTO-SUBMITTED" in run.log


def test_gemini_draft_forces_review_then_submits_on_approve(db_url, world, chrome):
    set_auto(db_url, True)
    make, holder = factory(chrome, extra=WHY)
    drafter = lambda job: (lambda q, options=None: "I enjoy turning LLM capabilities into clear requirements.")
    runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(db_url, decide=ApplicationState.approved), drafter_factory=drafter)
    job, app, fields, run = state(db_url, world)
    drafted = [f for f in fields if f.source == FieldSource.llm]
    assert len(drafted) == 1 and drafted[0].needs_review
    assert "waiting up to" in run.log and "SUBMITTED after your approval" in run.log and "AUTO-SUBMITTED" not in run.log
    assert app.state == ApplicationState.submitted and job.status == JobStatus.applied


def test_review_mode_never_submits_without_approval(db_url, world, chrome):
    set_auto(db_url, False)
    make, holder = factory(chrome)
    seen = {}
    pacing = FakePacing(db_url, decide=ApplicationState.skipped)
    original = runner.wait_for_decision

    def spy(session, app, page, *args, **kwargs):
        seen["submits_while_waiting"] = page.evaluate("window.__submits")
        return original(session, app, page, *args, **kwargs)

    runner.wait_for_decision = spy
    try:
        runner.run(db_url=db_url, browser_factory=make, pacing=pacing, drafter_factory=lambda job: None)
    finally:
        runner.wait_for_decision = original
    job, app, _, run = state(db_url, world)
    assert seen["submits_while_waiting"] == 0  # filled and stopped before Submit
    assert app.state == ApplicationState.skipped and job.status == JobStatus.docs_ready
    assert "auto-submit is off" in run.log


def hold_window_open(db_url):
    with Session(get_engine(db_url)) as s:
        put_setting(s, "park_incomplete", False)
        s.commit()


def test_unanswered_question_becomes_open_question(db_url, world, chrome):
    set_auto(db_url, True)
    hold_window_open(db_url)
    make, _ = factory(chrome, extra=WHY)
    runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(db_url, decide=ApplicationState.skipped),
               drafter_factory=lambda job: (lambda q, options=None: None))  # resume can't support an answer
    job, app, fields, run = state(db_url, world)
    assert app.state == ApplicationState.skipped and "form NOT ready: stuck on step 1" in run.log
    assert app.reason == "skipped_by_you" and any(f.value_used == "(empty: needs you)" for f in fields)
    with Session(get_engine(db_url)) as s:
        open_q = s.exec(select(Answer).where(Answer.question == "Why are you interested in this role?")).one()
    assert open_q.answer == "" and "needs-answer" in open_q.tags


def test_consent_box_is_never_ticked_and_blocks_approve_until_you_finish(db_url, world, chrome):
    from fastapi import HTTPException
    from jobpilot.api.routers.applications import decide

    set_auto(db_url, True)
    hold_window_open(db_url)
    with Session(get_engine(db_url)) as s:  # even an answer-bank "Yes" must not tick a consent box
        s.add(Answer(question="consent to collect, store, and process my data", answer="Yes", tags="t"))
        s.commit()
    make, holder = factory(chrome, extra=CONSENT_BOX)
    seen = {}

    class You(FakePacing):
        def _sleep(self, _seconds):
            page = holder["context"].pages[0]
            with Session(get_engine(db_url)) as s:
                app = s.exec(select(Application).where(Application.state == ApplicationState.pending_review)).first()
                if app is None:
                    return
                if "ticked" not in seen:  # first poll: the agent left it incomplete, Approve is refused
                    seen["ticked"] = page.evaluate("document.getElementById('ok').checked")
                    seen["reason"] = app.reason
                    try:
                        decide(app.id, ApplicationState.approved, s)
                    except HTTPException as error:
                        seen["approve"] = error.status_code
                    page.check("#ok")  # you tick it yourself and move the form on to Submit
                    page.click("#next")
                    page.click("#review")
                elif app.reason == "":  # runner noticed the Submit page: now you approve
                    app.state = ApplicationState.approved
                    s.add(app)
                    s.commit()

    runner.run(db_url=db_url, browser_factory=make, pacing=You(), drafter_factory=lambda job: None)
    job, app, fields, run = state(db_url, world)
    assert seen == {"ticked": False, "reason": "incomplete", "approve": 409}
    consent = [f for f in fields if f.value_used.startswith("(not ticked")]
    assert len(consent) == 1 and consent[0].field_name.startswith("Acme has my consent") and consent[0].needs_review
    assert "AUTO-SUBMITTED" not in run.log and "form NOT ready" in run.log
    assert "Approve is now available" in run.log and "SUBMITTED after your approval" in run.log
    assert app.state == ApplicationState.submitted and job.status == JobStatus.applied


def test_security_page_blocks_and_stops(db_url, world, chrome, monkeypatch):
    monkeypatch.setattr(runner.time, "monotonic", iter(range(0, 10**9, 10**6)).__next__)  # skip the 15-min hold
    make, _ = factory(chrome, redirect_to_checkpoint=True)
    with pytest.raises(runner.Blocked):
        runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(), drafter_factory=lambda job: None)
    job, app, _, run = state(db_url, world)
    assert app.state == ApplicationState.blocked and job.status == JobStatus.docs_ready
    assert run.status == RunStatus.error and "BLOCKED" in run.log


def test_not_signed_in_stops_before_touching_jobs(db_url, world, chrome):
    make, _ = factory(chrome, signed_in=False)
    with pytest.raises(RuntimeError, match="Not signed in"):
        runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(), drafter_factory=lambda job: None)
    assert state(db_url, world)[1] is None


def test_daily_cap_respected(db_url, world, chrome):
    with Session(get_engine(db_url)) as s:
        put_setting(s, "daily_apply_cap", 1)
        from jobpilot.db.models import utcnow
        other, _ = upsert_job(s, {"title": "x", "company": "Other", "url": "https://example.com/other", "status": "applied"})
        other.applied_at = utcnow()
        s.add(other)
        s.commit()
    make, _ = factory(chrome)
    runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(), drafter_factory=lambda job: None)
    assert state(db_url, world)[1] is None and "daily cap reached" in state(db_url, world)[3].log


def test_broken_page_is_skipped_not_stuck(db_url, world, chrome):
    from contextlib import contextmanager

    @contextmanager
    def make():
        context = chrome.new_context()
        context.add_cookies([{"name": "li_at", "value": "t", "domain": ".www.linkedin.com", "path": "/"}])
        context.route("https://www.linkedin.com/**", lambda route: route.abort("connectionreset"))
        yield context
        context.close()

    runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(), drafter_factory=lambda job: None)
    job, app, _, run = state(db_url, world)
    assert app.state == ApplicationState.skipped and run.status == RunStatus.ok and "FAILED to fill" in run.log
    with Session(get_engine(db_url)) as s:
        assert runner.select_jobs(s, None, 60)[0].id == world  # retried next run, not stuck


def test_not_easy_apply_is_remembered(db_url, world, chrome):
    from contextlib import contextmanager

    @contextmanager
    def make():
        context = chrome.new_context()
        context.add_cookies([{"name": "li_at", "value": "t", "domain": ".www.linkedin.com", "path": "/"}])
        context.route("https://www.linkedin.com/**", lambda route: route.fulfill(
            status=200, content_type="text/html", body="<main><h1>BA</h1><a href='#'>Apply on company website</a></main>"))
        yield context
        context.close()

    runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(), drafter_factory=lambda job: None)
    job, app, _, run = state(db_url, world)
    assert app.state == ApplicationState.skipped and app.reason == "not_easy_apply"
    with Session(get_engine(db_url)) as s:
        assert runner.select_jobs(s, None, 60) == []  # never re-opened


# ---- parking: fill what we can, save progress, move on --------------------------------

def second_job(db_url, n=2) -> int:
    with Session(get_engine(db_url)) as s:
        job, _ = upsert_job(s, {"title": "Product Analyst", "company": f"Beta{n}", "source": "linkedin",
                                "url": f"https://www.linkedin.com/jobs/view/430000000{n}/", "jd_text": "LLM requirements " * 30,
                                "score": 75, "status": "docs_ready"})
        s.commit()
        return job.id


def test_stuck_form_is_parked_with_draft_saved_and_nothing_submitted(db_url, world, chrome):
    make, holder = factory(chrome, extra=WHY)
    runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(), drafter_factory=lambda job: (lambda q, options=None: None))
    job, app, fields, run = state(db_url, world)
    assert app.state == ApplicationState.pending_review and app.reason == "draft_saved"
    assert holder["save"] == 1 and holder["discard"] == 0            # Save clicked, Discard never
    assert holder["context"].pages == [] or all(p.is_closed() for p in holder["context"].pages)   # tab closed, not held open
    names = {f.field_name: f for f in fields}
    assert names["Mobile phone number"].value_used == "9000000000" and not names["Mobile phone number"].needs_review
    blank = names["Why are you interested in this role?"]
    assert blank.value_used == "(empty: needs you)" and blank.needs_review    # the blank you fill in yourself
    assert job.status == JobStatus.docs_ready and "PARKED" in run.log and "draft is saved" in run.log
    assert run.status == RunStatus.ok and app.screenshot_path


def test_parked_form_when_linkedin_offers_no_save_prompt_is_marked_unsaved(db_url, world, chrome):
    make, holder = factory(chrome, extra=WHY, save_prompt=False)
    runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(), drafter_factory=lambda job: (lambda q, options=None: None))
    _, app, fields, run = state(db_url, world)
    assert app.reason == "draft_unsaved" and holder["save"] == 0 and holder["discard"] == 0
    assert "could not be saved" in run.log and any(f.value_used == "(empty: needs you)" for f in fields)


def test_consent_box_form_is_parked_and_box_stays_unticked(db_url, world, chrome):
    make, _ = factory(chrome, extra=CONSENT_BOX)
    runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(), drafter_factory=lambda job: None)
    _, app, fields, _ = state(db_url, world)
    assert app.state == ApplicationState.pending_review and app.reason in runner.PARKED
    consent = [f for f in fields if f.value_used.startswith("(not ticked")]
    assert len(consent) == 1 and consent[0].needs_review


def test_a_parked_job_does_not_hold_up_the_next_one(db_url, world, chrome):
    other = second_job(db_url)
    make, holder = factory(chrome, extra=WHY)
    runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(), drafter_factory=lambda job: (lambda q, options=None: None))
    with Session(get_engine(db_url)) as s:
        apps = s.exec(select(Application)).all()
        assert sorted(a.job_id for a in apps) == sorted([world, other]) and all(a.reason == "draft_saved" for a in apps)
        assert runner.select_jobs(s, None, 60) == []                   # parked jobs are not opened again
    assert holder["save"] == 2


def test_nothing_is_ever_submitted_while_parking(db_url, world, chrome):
    set_auto(db_url, True)                                            # even with auto-submit on
    make, holder = factory(chrome, extra=WHY)
    runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(), drafter_factory=lambda job: (lambda q, options=None: None))
    _, app, _, run = state(db_url, world)
    assert "SUBMITTED" not in run.log and app.state == ApplicationState.pending_review


def test_fully_answered_form_is_still_staged_for_approval_not_parked(db_url, world, chrome):
    make, _ = factory(chrome)
    runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(db_url, decide=ApplicationState.skipped),
               drafter_factory=lambda job: (lambda q, options=None: "I am keen on AI products."))
    _, app, _, run = state(db_url, world)
    assert "PARKED" not in run.log and "form filled, stopped before Submit" in run.log and app.reason == "skipped_by_you"


def test_review_api_parked_application_flow(db_url, world, chrome):
    from fastapi import HTTPException

    from jobpilot.api.routers.applications import decide, mark_submitted

    make, _ = factory(chrome, extra=WHY)
    runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(), drafter_factory=lambda job: (lambda q, options=None: None))
    with Session(get_engine(db_url)) as s:
        app = s.exec(select(Application)).one()
        with pytest.raises(HTTPException) as err:
            decide(app.id, ApplicationState.approved, s)               # the agent is not holding any form
        assert err.value.status_code == 409 and "saved for you to finish" in err.value.detail
        out = mark_submitted(app.id, s)                                # you submitted it on LinkedIn yourself
        assert out["state"] == "submitted" and out["reason"] == ""
        job = s.get(Job, world)
        assert job.status == JobStatus.applied and job.applied_at is not None
        with pytest.raises(HTTPException):
            mark_submitted(app.id, s)                                  # not twice


def test_skip_works_on_a_parked_application(db_url, world, chrome):
    from jobpilot.api.routers.applications import decide

    make, _ = factory(chrome, extra=WHY)
    runner.run(db_url=db_url, browser_factory=make, pacing=FakePacing(), drafter_factory=lambda job: (lambda q, options=None: None))
    with Session(get_engine(db_url)) as s:
        app = s.exec(select(Application)).one()
        assert decide(app.id, ApplicationState.skipped, s)["state"] == "skipped"
