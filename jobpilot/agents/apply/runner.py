"""
Apply agent (LinkedIn Easy Apply + Naukri). One job at a time, in your signed-in Chrome profile.

    python -m jobpilot.agents.apply                 # docs_ready LinkedIn/Naukri jobs >= threshold, up to today's cap
    python -m jobpilot.agents.apply --job-id 132    # one job ("Queue for apply" in the dashboard)
    python -m jobpilot.agents.apply --review-only   # never auto-submit this run

Per job: open the posting, fill Easy Apply with the legacy filler (never clicks
Submit), screenshot, record every field and where it came from. Then:

- auto-submit ONLY if the Settings switch is on, the job is docs_ready and
  >= threshold, and every field came from your resume/profile or answer bank;
- otherwise pause in the Review queue and wait (same tab) for Approve/Skip;
- sign-in, checkpoint, CAPTCHA or OTP page: stop, mark blocked, notify you,
  and leave the browser on that page. Never typed past, never solved.

Stuck forms are PARKED (setting park_incomplete, on by default): when a form can't reach its Submit page
because an answer isn't in your answer bank (or a consent box is waiting), the agent keeps everything it
filled, records each field and each blank in the Review queue, tries to save LinkedIn's draft ("Save this
application?" -> Save, never Discard), closes the tab and moves on to the next job. You finish the blanks on
LinkedIn / Naukri yourself, submit, then click "I submitted it" in Review. Switch it off to make the agent
hold the window open and wait for you instead.

Naukri: its "Apply" button IS the submit, so the agent only reads the posting,
screenshots it and waits for you (or auto-submits under the same rules). After
Apply, chatbot questions are answered only from your answer bank; anything else
stops it and leaves the chat for you (see naukri.py).

Daily cap, randomised 20-90 s pauses, one tab at a time. Passwords are never typed.
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from sqlmodel import Session, select

from jobpilot.agents._run import RunRecorder, agent_run
from jobpilot.agents.apply.answers import AnswerBank, load_contact, make_drafter
from jobpilot.agents.apply.filler import easy_apply_module, make_filler_class
from jobpilot.agents.apply.naukri import READY, make_naukri_applier, naukri_module
from jobpilot.api.deps import ist_date, today_ist
from jobpilot.config import DATA_DIR, browser_profile_dir, get_config
from jobpilot.db.models import (
    Actor,
    AgentName,
    Application,
    ApplicationField,
    ApplicationState,
    FieldSource,
    Job,
    JobStatus,
    utcnow,
)
from jobpilot.db.dedupe import portal_of
from jobpilot.db.repo import get_setting, set_status
from jobpilot.db.session import get_engine
from jobpilot.llm.gemini import GeminiClient
from jobpilot.notify import notify
from jobpilot.resume import load_resume

SECURITY_MARKERS = ("checkpoint", "challenge", "captcha", "authwall", "/login", "/uas/")
SOURCES = ("linkedin", "naukri")
NEVER_RETRY = ("not_easy_apply", "external")  # the portal sends these to the company's own site
PARKED = ("draft_saved", "draft_unsaved")  # Application.reason of a form saved for you to finish
DISMISS_BUTTON = re.compile(r"^\s*(dismiss|close)\s*$", re.IGNORECASE)
SAVE_BUTTON = re.compile(r"^\s*save( application| draft)?\s*$", re.IGNORECASE)  # never matches "Discard"
SESSION_COOKIE = "li_at"
SCREENSHOTS = DATA_DIR / "screenshots"
OPEN_STATES = (ApplicationState.filling, ApplicationState.pending_review, ApplicationState.approved,
               ApplicationState.submitted, ApplicationState.blocked)


class Blocked(RuntimeError):
    """LinkedIn wants a human (sign-in, checkpoint, CAPTCHA, OTP)."""


class Pacing:
    def __init__(self, low: float = 20, high: float = 90, sleep: Callable[[float], None] = time.sleep,
                 poll_seconds: float = 3.0) -> None:
        self.low, self.high, self.sleep, self.poll_seconds = low, high, sleep, poll_seconds

    def between_jobs(self) -> float:
        seconds = random.uniform(self.low, self.high)
        self.sleep(seconds)
        return seconds


def check_security(page: Any) -> None:
    url = (page.url or "").lower()
    if any(marker in url for marker in SECURITY_MARKERS):
        raise Blocked(f"LinkedIn showed a sign-in/security page ({page.url})")


@contextmanager
def signed_in_browser() -> Iterator[Any]:
    """The agent's persistent Chrome profile (already signed in), visible window."""
    from playwright.sync_api import sync_playwright

    profile = browser_profile_dir()
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch_persistent_context(
                str(profile), channel="chrome", headless=False, viewport=None, locale="en-IN", timezone_id="Asia/Kolkata")
        except Exception as error:
            raise RuntimeError(f"Could not open Chrome with the agent profile ({str(error)[:160]}). "
                               "Close any window the legacy agent left open and try again.") from error
        try:
            yield browser
        finally:
            try:
                browser.close()
            except Exception:
                pass


def is_signed_in(browser: Any, source: str = "linkedin") -> bool:
    if source == "naukri":
        return naukri_module().is_signed_in(browser)
    return any(c["name"] == SESSION_COOKIE and c["value"] for c in browser.cookies("https://www.linkedin.com"))


LOGIN_HINT = {"linkedin": "python -m jobpilot.agents.apply.login linkedin", "naukri": "python -m jobpilot.agents.apply.login naukri"}


def portal(job: Job) -> str:
    """Which apply flow: from the posting link, falling back to the stored source."""
    return portal_of(job.url) or job.source


def applied_today(session: Session) -> int:
    today = today_ist()
    rows = session.exec(select(Job.applied_at).where(Job.is_example.is_(False), Job.applied_at.is_not(None)))
    return sum(1 for value in rows if ist_date(value) == today)


MAX_RELAUNCHES = 3  # times one run reopens Chrome after it was closed under it


def browser_closed(error: Exception) -> bool:
    """Playwright's TargetClosedError: the window (or Chrome) went away mid-job."""
    return type(error).__name__ == "TargetClosedError" or "has been closed" in str(error)


def close_unfinished(session: Session, job_id: int) -> int:
    """An application left in `filling` by a dead browser blocks its job from the queue; close it as skipped."""
    stuck = session.exec(select(Application).where(Application.job_id == job_id,
                                                  Application.state == ApplicationState.filling)).all()
    for app in stuck:
        app.state, app.reason, app.decided_at = ApplicationState.skipped, "browser_closed", utcnow()
        session.add(app)
    session.commit()
    return len(stuck)



def supersede_parked(session: Session, job_id: int) -> int:
    """An explicit re-run of a job whose application was parked retires the old entry (state skipped, reason
    'superseded'). That is not a decision of yours, so it never counts as a skip in the readiness statistics."""
    old = session.exec(select(Application).where(Application.job_id == job_id,
                                                 Application.state == ApplicationState.pending_review,
                                                 Application.reason.in_(PARKED))).all()
    for app in old:
        app.state, app.reason, app.decided_at = ApplicationState.skipped, "superseded", utcnow()
        session.add(app)
    session.commit()
    return len(old)


def select_jobs(session: Session, job_id: int | None, threshold: float) -> list[Job]:
    busy = {a.job_id for a in session.exec(select(Application).where(
        Application.state.in_(OPEN_STATES) | Application.reason.in_(NEVER_RETRY)))}
    if job_id is not None:
        job = session.get(Job, job_id)
        return [job] if job and job.id not in busy else []
    query = select(Job).where(Job.status == JobStatus.docs_ready, Job.source.in_(SOURCES), Job.is_example.is_(False),
                              Job.score >= threshold).order_by(Job.score.desc())
    return [job for job in session.exec(query) if job.id not in busy]


def save_fields(session: Session, app: Application, records: list[Any], unknown: list[str],
                consent: list[str] = ()) -> None:
    for r in records:
        session.add(ApplicationField(application_id=app.id, step=r.step, field_name=r.name, value_used=r.value,
                                     source=r.source, needs_review=r.needs_review))
    for question in unknown:  # required, still empty: you complete these in the browser tab
        session.add(ApplicationField(application_id=app.id, step=0, field_name=question, value_used="(empty: needs you)",
                                     source=FieldSource.answer_bank, needs_review=True))
    for question in consent:  # consent / terms: the agent never agrees on your behalf
        session.add(ApplicationField(application_id=app.id, step=0, field_name=question,
                                     value_used="(not ticked: only you can agree to this)",
                                     source=FieldSource.answer_bank, needs_review=True))


def at_submit_step(filler: Any) -> bool:
    """True only when the dialog is showing LinkedIn's 'Submit application' button."""
    try:
        dialog = filler._dialog
        return dialog is not None and filler._button(dialog, easy_apply_module().SUBMIT_BUTTON) is not None
    except Exception:
        return False


def stuck_questions(filler: Any) -> list[str]:
    """Questions LinkedIn flagged 'required' on the step the form is stuck on."""
    try:
        return [q for q in filler._error_questions(filler._dialog) if q] if filler._dialog is not None else []
    except Exception:
        return []


def save_draft(page: Any, filler: Any) -> bool:
    """Close the Easy Apply dialog and answer LinkedIn's "Save this application?" prompt with Save.

    True only if that prompt appeared and Save was clicked. Never clicks Discard. If LinkedIn shows no prompt
    (nothing worth saving, or a layout change) this returns False and the progress is only in JobPilot's record.
    """
    try:
        dialog = filler._dialog
        if dialog is None or page.is_closed():
            return False
        close = dialog.get_by_role("button", name=DISMISS_BUTTON).first
        if close.count() == 0:
            close = page.locator("button[aria-label='Dismiss'], button.artdeco-modal__dismiss").first
        if close.count() == 0:
            return False
        close.click(timeout=5_000)
        save = page.get_by_role("button", name=SAVE_BUTTON).first
        save.wait_for(state="visible", timeout=5_000)
        save.click(timeout=5_000)
        page.wait_for_timeout(1_000)
        return True
    except Exception:
        return False


def park(session: Session, app: Application, job: Job, page: Any, rec: RunRecorder, *, saved: bool,
         portal_name: str, filled: int, blanks: list[str]) -> str:
    """Leave a half-filled application for you: state pending_review, reason draft_saved / draft_unsaved, tab closed."""
    app.state, app.reason = ApplicationState.pending_review, "draft_saved" if saved else "draft_unsaved"
    session.add(app)
    session.commit()
    try:
        if not page.is_closed():
            page.close()
    except Exception:
        pass
    where = ("LinkedIn's draft is saved" if saved else
             "LinkedIn's draft could not be saved; re-enter the filled values from Review" if portal_name == "LinkedIn" else
             "Naukri keeps the chat on its side, but reopen it soon")
    left = "; ".join(blanks[:4]) or "a required field the portal did not name"
    notify("JobPilot: saved for you to finish", f"{job.title} at {job.company}: {len(blanks)} blank(s) need you.")
    rec.log(f"  #{job.id} {job.title} | {job.company}: PARKED. {filled} field(s) filled and recorded, {where}. "
            f"Blank for you: {left[:300]}. Finish it on {portal_name}, submit, then click 'I submitted it' in Review.")
    return "parked"


def mark_submitted(session: Session, app: Application, job: Job, by: Actor) -> None:
    app.state, app.decided_at = ApplicationState.submitted, app.decided_at or utcnow()
    if app.reason == "incomplete":  # finished after all
        app.reason = ""
    session.add(app)
    set_status(session, job, JobStatus.applied, actor=by)
    session.commit()


def wait_for_decision(session: Session, app: Application, page: Any, filler: Any, minutes: float, pacing: Pacing,
                      rec: RunRecorder, sent: Callable[[Any], bool]) -> str:
    """Poll the Review queue decision while the filled form stays open in this tab."""
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        session.refresh(app)
        if app.state == ApplicationState.approved:
            return "approved"
        if app.state == ApplicationState.skipped:
            return "skipped"
        try:
            if page.is_closed():
                return "closed"
            if sent(page):  # you submitted it yourself in the browser
                return "submitted_by_you"
            if app.reason == "incomplete" and at_submit_step(filler):  # you finished the missing steps by hand
                app.reason = ""
                session.add(app)
                session.commit()
                rec.log("  you completed the form up to its Submit page: Approve is now available in the Review queue")
        except Exception:
            return "closed"
        pacing.sleep(pacing.poll_seconds)
    return "timeout"


def process_job(session: Session, browser: Any, job: Job, rec: RunRecorder, pacing: Pacing, *,
                auto_submit: bool, threshold: float, wait_minutes: float, drafter_factory: Callable[[Job], Any],
                park_incomplete: bool = False) -> str:
    easy_apply = easy_apply_module()
    app = Application(job_id=job.id, state=ApplicationState.filling)
    session.add(app)
    session.commit()

    page = browser.new_page()
    try:
        page.goto(job.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        check_security(page)
        filler = make_filler_class()(page, AnswerBank(session, load_contact()), drafter_factory(job))
        result = filler.prepare()  # fills every step and STOPS on the Submit step
        check_security(page)
    except Blocked:
        app.state, app.reason = ApplicationState.blocked, "blocked"
        session.add(app)
        session.commit()
        raise
    except Exception as error:  # a broken page must not stop the run or leave the job stuck in "filling"
        session.rollback()
        app.state, app.decided_at, app.reason = ApplicationState.skipped, utcnow(), "failed"
        session.add(app)
        session.commit()
        try:
            page.close()
        except Exception:
            pass
        rec.log(f"  #{job.id} {job.company}: FAILED to fill ({str(error).splitlines()[0][:160]}); skipped, run again to retry")
        return "failed"
    session.commit()  # answer-bank usage counts and new open questions

    if result.status == easy_apply.NOT_EASY_APPLY:
        app.state, app.decided_at, app.reason = ApplicationState.skipped, utcnow(), "not_easy_apply"
        session.add(app)
        session.commit()
        page.close()
        rec.log(f"  #{job.id} {job.company}: not an Easy Apply job; apply on the company site yourself")
        return "not_easy_apply"
    if result.status == easy_apply.ALREADY_APPLIED:
        mark_submitted(session, app, job, Actor.agent)
        page.close()
        rec.log(f"  #{job.id} {job.company}: LinkedIn says you already applied; marked applied")
        return "already_applied"

    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    shot = SCREENSHOTS / f"application_{app.id}.png"
    page.screenshot(path=str(shot), full_page=False)
    app.screenshot_path = str(shot)
    ready = at_submit_step(filler)
    consent = list(getattr(filler, "consent", []))
    unknown = [q for q in result.unknown_questions if q not in consent]
    if not ready:  # LinkedIn kept the form on an earlier step: collect what it flagged as required
        unknown += [q for q in stuck_questions(filler) if q not in unknown and q not in consent]
    app.reason = "" if ready else "incomplete"
    save_fields(session, app, filler.records, unknown, consent)
    session.commit()

    complete = ready and result.status == easy_apply.READY_TO_SUBMIT and not unknown and not consent
    drafted = sum(1 for r in filler.records if r.needs_review)
    trusted = complete and drafted == 0
    eligible = job.status == JobStatus.docs_ready and (job.score or 0) >= threshold
    summary = (f"{len(filler.records)} field(s): {sum(r.source.value == 'answer_bank' for r in filler.records)} answer bank, "
               f"{sum(r.source.value == 'resume' for r in filler.records)} resume/profile, {drafted} Gemini draft(s), "
               f"{len(unknown)} empty, {len(consent)} consent box(es) left for you")
    if ready:
        rec.log(f"  #{job.id} {job.title} | {job.company}: form filled, stopped before Submit ({summary})")
    else:
        left = "; ".join((consent + unknown)[:4]) or "a required field LinkedIn didn't name"
        rec.log(f"  #{job.id} {job.title} | {job.company}: form NOT ready: stuck on step {result.steps}, before the Submit page "
                f"({summary}). Needs you: {left[:300]}. Finish it yourself in the agent's browser window.")

    if park_incomplete and not ready:
        blanks = [f for f in consent + unknown if f]
        return park(session, app, job, page, rec, saved=save_draft(page, filler), portal_name="LinkedIn",
                    filled=len(filler.records), blanks=blanks)

    if auto_submit and trusted and eligible:
        if filler.submit():
            mark_submitted(session, app, job, Actor.agent)
            rec.log(f"  #{job.id} {job.company}: AUTO-SUBMITTED (every answer from your resume/answer bank)")
            page.close()
            return "submitted"
        rec.log(f"  #{job.id} {job.company}: Submit not confirmed by LinkedIn; sending to review")

    reason = ("form stuck before Submit: finish it in the browser window" if not ready
              else "consent box only you can tick" if consent
              else "auto-submit is off" if not auto_submit else "Gemini drafted answers" if drafted
              else "required fields are empty" if not complete else "job not docs_ready / below threshold")
    app.state = ApplicationState.pending_review
    session.add(app)
    session.commit()
    notify("JobPilot: review needed" if ready else "JobPilot: finish this form yourself",
           f"{job.title} at {job.company} {'is filled and waiting for you' if ready else 'needs you in the browser window'} ({reason}).")
    rec.log(f"  #{job.id} {job.company}: waiting up to {wait_minutes:g} min for "
            f"{'your decision in the Review queue' if ready else 'you to finish it in the browser window (or Skip in Review)'} ({reason})")

    decision = wait_for_decision(session, app, page, filler, wait_minutes, pacing, rec, easy_apply.EasyApplyFiller.application_sent)
    if decision == "approved":
        if filler.submit():
            mark_submitted(session, app, job, Actor.user)
            rec.log(f"  #{job.id} {job.company}: SUBMITTED after your approval")
            page.close()
            return "submitted"
        app.state = ApplicationState.pending_review
        session.add(app)
        session.commit()
        rec.log(f"  #{job.id} {job.company}: approved, but LinkedIn didn't confirm the submit; finish it in the open tab")
        return "unconfirmed"
    if decision == "submitted_by_you":
        mark_submitted(session, app, job, Actor.user)
        rec.log(f"  #{job.id} {job.company}: you submitted it in the browser; recorded as applied")
        return "submitted"
    return close_skipped(session, app, page, job, rec, decision)


def close_skipped(session: Session, app: Application, page: Any, job: Job, rec: RunRecorder, decision: str) -> str:
    app.state, app.decided_at = ApplicationState.skipped, utcnow()
    app.reason = {"skipped": "skipped_by_you", "timeout": "timeout"}.get(decision, "tab_closed")
    session.add(app)
    session.commit()
    if not page.is_closed():
        page.close()
    rec.log(f"  #{job.id} {job.company}: {'skipped by you' if decision == 'skipped' else 'no decision in time; run Apply for this job again later' if decision == 'timeout' else 'tab closed'}")
    return decision


def process_naukri_job(session: Session, browser: Any, job: Job, rec: RunRecorder, pacing: Pacing, *,
                       auto_submit: bool, threshold: float, wait_minutes: float, park_incomplete: bool = False) -> str:
    nk = naukri_module()
    app = Application(job_id=job.id, state=ApplicationState.filling)
    session.add(app)
    session.commit()

    def blocked(error: Exception) -> Blocked:
        app.state, app.reason = ApplicationState.blocked, "blocked"
        session.add(app)
        session.commit()
        return Blocked(f"Naukri showed a sign-in/OTP/CAPTCHA page ({str(error)[:160]})")

    page = browser.new_page()
    try:
        page.goto(job.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        applier = make_naukri_applier()(page, AnswerBank(session, load_contact()))
        status = applier.precheck()
    except nk.NaukriSecurityError as error:
        raise blocked(error)
    except Exception as error:
        session.rollback()
        app.state, app.decided_at, app.reason = ApplicationState.skipped, utcnow(), "failed"
        session.add(app)
        session.commit()
        try:
            page.close()
        except Exception:
            pass
        rec.log(f"  #{job.id} {job.company}: FAILED to open the Naukri posting ({str(error).splitlines()[0][:160]}); skipped")
        return "failed"

    if status == nk.ALREADY_APPLIED:
        mark_submitted(session, app, job, Actor.agent)
        page.close()
        rec.log(f"  #{job.id} {job.company}: Naukri says you already applied; marked applied")
        return "already_applied"
    if status == nk.EXTERNAL:
        app.state, app.decided_at, app.reason = ApplicationState.skipped, utcnow(), "external"
        session.add(app)
        session.commit()
        page.close()
        rec.log(f"  #{job.id} {job.company}: Naukri sends this one to the company's site; apply there yourself")
        return "external"
    if status != READY:  # NOT_SIGNED_IN: a login wall is never typed past
        raise blocked(RuntimeError("Naukri asks you to log in to apply"))

    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    shot = SCREENSHOTS / f"application_{app.id}.png"
    page.screenshot(path=str(shot), full_page=False)
    app.screenshot_path = str(shot)
    session.add(ApplicationField(application_id=app.id, step=1, field_name="Naukri profile and resume on file",
                                 value_used="sent by Naukri when Apply is clicked", source=FieldSource.resume))
    session.commit()

    eligible = job.status == JobStatus.docs_ready and (job.score or 0) >= threshold
    actor = Actor.agent
    if not (auto_submit and eligible):
        reason = "auto-submit is off" if not auto_submit else "job not docs_ready / below threshold"
        app.state = ApplicationState.pending_review
        session.add(app)
        session.commit()
        notify("JobPilot: review needed", f"{job.title} at {job.company} (Naukri) is ready to apply ({reason}).")
        rec.log(f"  #{job.id} {job.title} | {job.company}: Naukri posting checked (signed in, Apply button present). "
                f"Nothing sent yet: on Naukri, Apply IS the submit. Waiting up to {wait_minutes:g} min for you ({reason})")
        decision = wait_for_decision(session, app, page, applier, wait_minutes, pacing, rec, applier.confirmed)
        if decision == "submitted_by_you":
            mark_submitted(session, app, job, Actor.user)
            rec.log(f"  #{job.id} {job.company}: you applied in the browser; recorded as applied")
            return "submitted"
        if decision != "approved":
            return close_skipped(session, app, page, job, rec, decision)
        actor = Actor.user

    try:
        outcome = applier.submit()
    except nk.NaukriSecurityError as error:
        raise blocked(error)
    save_fields(session, app, applier.records, applier.unknown, applier.consent)
    if applier.follow and applier.follow != "not found":
        session.add(ApplicationField(application_id=app.id, step=1, field_name=f"Follow {job.company} as you apply",
                                     value_used={"unticked": "unticked by the agent", "not ticked": "was not ticked"}.get(
                                         applier.follow, "left ticked (the agent couldn't untick it)"),
                                     source=FieldSource.resume, needs_review=applier.follow == "left ticked"))
    session.commit()
    if outcome == nk.SUBMITTED:
        mark_submitted(session, app, job, actor)
        page.close()
        rec.log(f"  #{job.id} {job.company}: APPLIED on Naukri ({'auto-submit' if actor == Actor.agent else 'after your approval'}; "
                f"{len(applier.records)} chatbot answer(s) from your answer bank)")
        return "submitted"

    # Apply was clicked but Naukri wants more: hand the open chat to you.
    left = "; ".join((applier.consent + applier.unknown)[:4]) or "Naukri didn't confirm the application"
    if park_incomplete:
        return park(session, app, job, page, rec, saved=False, portal_name="Naukri", filled=len(applier.records),
                    blanks=[q for q in applier.consent + applier.unknown if q])
    app.state, app.reason = ApplicationState.pending_review, "incomplete"
    session.add(app)
    session.commit()
    notify("JobPilot: finish this Naukri application", f"{job.title} at {job.company}: {left[:120]}")
    rec.log(f"  #{job.id} {job.company}: Naukri needs you: {left[:300]}. Answer it in the agent's browser window "
            f"(waiting up to {wait_minutes:g} min).")
    decision = wait_for_decision(session, app, page, applier, wait_minutes, pacing, rec, applier.confirmed)
    if decision == "submitted_by_you":
        mark_submitted(session, app, job, Actor.user)
        rec.log(f"  #{job.id} {job.company}: you finished it in the browser; recorded as applied")
        return "submitted"
    return close_skipped(session, app, page, job, rec, decision)


def run(job_id: int | None = None, review_only: bool = False, limit: int | None = None, db_url: str | None = None,
        run_id: int | None = None, max_jobs: int | None = None,
        browser_factory: Callable[[], Any] = signed_in_browser, pacing: Pacing | None = None,
        drafter_factory: Callable[..., Any] | None = None) -> None:
    pacing = pacing or Pacing()
    with agent_run(AgentName.apply, db_url, run_id) as rec, Session(get_engine(db_url)) as session:
        threshold = float(get_setting(session, "threshold"))
        cap = int(get_setting(session, "daily_apply_cap"))
        auto_submit = bool(get_setting(session, "auto_submit")) and not review_only
        wait_minutes = float(get_setting(session, "review_wait_minutes"))
        park_incomplete = bool(get_setting(session, "park_incomplete"))
        budget = cap - applied_today(session)
        if job_id is not None and supersede_parked(session, job_id):
            rec.log(f"#{job_id}: the earlier saved application is replaced by this run (it picks up where LinkedIn's saved draft left off)")
        jobs = select_jobs(session, job_id, threshold)
        rec.log(f"Auto-submit {'ON' if auto_submit else 'OFF'} · threshold {threshold:g} · today {cap - budget}/{cap} applied · "
                f"{len(jobs)} job(s) queued" + (f" · stop after {limit} filled form(s)" if limit else "")
                + (" · stuck forms are parked for you" if park_incomplete else ""))
        filled = 0
        if budget <= 0 or not jobs:
            rec.log("Nothing to do (daily cap reached or no docs_ready LinkedIn/Naukri jobs).")
            return
        resume = load_resume()
        if drafter_factory is None:
            def drafter_factory(job: Job):
                return make_drafter(lambda: GeminiClient(on_retry=rec.log), session, resume, job, rec.log)

        queue = list(jobs)
        checked = done = False
        started = relaunches = 0
        attempts: dict[int, int] = {}
        while queue and not done:
            try:
                with browser_factory() as browser:
                    if not checked:
                        signed_out = {src for src in {portal(job) for job in queue} if not is_signed_in(browser, src)}
                        for src in sorted(signed_out):
                            rec.log(f"Not signed in to {src} in the agent profile; skipping its jobs. "
                                    f"In E:\\JobPilot run: {LOGIN_HINT.get(src, '')} (you type the password).")
                        queue = [job for job in queue if portal(job) not in signed_out]
                        if not queue:
                            raise RuntimeError(f"Not signed in to {', '.join(sorted(signed_out))} in the agent profile. In "
                                               f"E:\\JobPilot run: {' / '.join(LOGIN_HINT.get(s, '') for s in sorted(signed_out))}")
                        checked = True
                    while queue:
                        job = queue[0]
                        if budget <= 0:
                            rec.log(f"Daily cap of {cap} reached; the rest wait for tomorrow")
                            done = True
                            break
                        if limit and filled >= limit:
                            rec.log(f"Reached the limit of {limit} filled form(s) for this run")
                            done = True
                            break
                        if max_jobs and len(attempts) >= max_jobs and job.id not in attempts:
                            rec.log(f"Reached the limit of {max_jobs} job(s) attempted in this run")
                            done = True
                            break
                        if started:
                            rec.log(f"Pausing {pacing.between_jobs():.0f}s before the next job")
                        started += 1
                        attempts[job.id] = attempts.get(job.id, 0) + 1
                        if attempts[job.id] == 1:
                            rec.processed += 1
                        try:
                            if portal(job) == "naukri":
                                outcome = process_naukri_job(session, browser, job, rec, pacing, auto_submit=auto_submit,
                                                             threshold=threshold, wait_minutes=wait_minutes,
                                                             park_incomplete=park_incomplete)
                            else:
                                outcome = process_job(session, browser, job, rec, pacing, auto_submit=auto_submit,
                                                      threshold=threshold, wait_minutes=wait_minutes,
                                                      drafter_factory=drafter_factory, park_incomplete=park_incomplete)
                        except Blocked as error:
                            notify("JobPilot: blocked", f"{error}. The browser is left on that page for you.")
                            rec.log(f"BLOCKED at #{job.id} {job.company}: {error}. Stopping; the window stays open for 15 min.")
                            deadline = time.monotonic() + 15 * 60
                            while time.monotonic() < deadline and browser.pages:
                                pacing.sleep(5)
                            raise
                        queue.pop(0)
                        if outcome not in ("not_easy_apply", "external", "already_applied", "failed"):
                            filled += 1
                        if outcome == "submitted":
                            rec.found += 1
                            budget -= 1
            except Blocked:
                raise
            except Exception as error:
                if not browser_closed(error) or relaunches >= MAX_RELAUNCHES:
                    raise
                relaunches += 1
                session.rollback()
                gone = queue[0] if queue else None
                if gone is not None:
                    close_unfinished(session, gone.id)
                    if attempts.get(gone.id, 0) >= 2:
                        queue.pop(0)
                        rec.log(f"  #{gone.id} {gone.company}: the browser closed twice on this job; leaving it for a later run")
                rec.log(f"The browser was closed ({str(error).splitlines()[0][:100]}); relaunching it "
                        f"({relaunches}/{MAX_RELAUNCHES}) and continuing with {len(queue)} job(s)")
                pacing.sleep(5)

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job-id", type=int, help="Apply to this one job")
    parser.add_argument("--review-only", action="store_true", help="Never auto-submit in this run")
    parser.add_argument("--limit", type=int, help="Stop after this many forms were filled (non-Easy-Apply jobs don't count)")
    parser.add_argument("--max-jobs", type=int, help="Stop after this many jobs were attempted, skipped ones included (this run only)")
    parser.add_argument("--pause", type=float, nargs=2, metavar=("LOW", "HIGH"),
                        help="Seconds to pause between jobs, random between LOW and HIGH (this run only; default 20 90)")
    parser.add_argument("--run-id", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    pacing = Pacing(low=args.pause[0], high=args.pause[1]) if args.pause else None
    try:
        run(job_id=args.job_id, review_only=args.review_only, limit=args.limit, run_id=args.run_id,
            max_jobs=args.max_jobs, pacing=pacing)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
