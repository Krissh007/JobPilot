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

Naukri: its "Apply" button IS the submit, so the agent only reads the posting,
screenshots it and waits for you (or auto-submits under the same rules). After
Apply, chatbot questions are answered only from your answer bank; anything else
stops it and leaves the chat for you (see naukri.py).

Daily cap, randomised 20-90 s pauses, one tab at a time. Passwords are never typed.
"""

from __future__ import annotations

import argparse
import random
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
from jobpilot.config import DATA_DIR, get_config
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
from jobpilot.db.repo import get_setting, set_status
from jobpilot.db.session import get_engine
from jobpilot.llm.gemini import GeminiClient
from jobpilot.notify import notify
from jobpilot.resume import load_resume

SECURITY_MARKERS = ("checkpoint", "challenge", "captcha", "authwall", "/login", "/uas/")
SOURCES = ("linkedin", "naukri")
NEVER_RETRY = ("not_easy_apply", "external")  # the portal sends these to the company's own site
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
    """Your legacy agent's persistent Chrome profile (already signed in), visible window."""
    from playwright.sync_api import sync_playwright

    profile = Path(get_config().legacy_agent_dir) / "data" / "browser_profile"
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


LOGIN_HINT = {"linkedin": "python -m src.apply.linkedin login", "naukri": "python -m src.apply.naukri login"}


def applied_today(session: Session) -> int:
    today = today_ist()
    rows = session.exec(select(Job.applied_at).where(Job.is_example.is_(False), Job.applied_at.is_not(None)))
    return sum(1 for value in rows if ist_date(value) == today)


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


def mark_submitted(session: Session, app: Application, job: Job, by: Actor) -> None:
    app.state, app.decided_at = ApplicationState.submitted, app.decided_at or utcnow()
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
                auto_submit: bool, threshold: float, wait_minutes: float, drafter_factory: Callable[[Job], Any]) -> str:
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
                       auto_submit: bool, threshold: float, wait_minutes: float) -> str:
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
    session.commit()
    if outcome == nk.SUBMITTED:
        mark_submitted(session, app, job, actor)
        page.close()
        rec.log(f"  #{job.id} {job.company}: APPLIED on Naukri ({'auto-submit' if actor == Actor.agent else 'after your approval'}; "
                f"{len(applier.records)} chatbot answer(s) from your answer bank)")
        return "submitted"

    # Apply was clicked but Naukri wants more: hand the open chat to you.
    left = "; ".join((applier.consent + applier.unknown)[:4]) or "Naukri didn't confirm the application"
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
        run_id: int | None = None,
        browser_factory: Callable[[], Any] = signed_in_browser, pacing: Pacing | None = None,
        drafter_factory: Callable[..., Any] | None = None) -> None:
    pacing = pacing or Pacing()
    with agent_run(AgentName.apply, db_url, run_id) as rec, Session(get_engine(db_url)) as session:
        threshold = float(get_setting(session, "threshold"))
        cap = int(get_setting(session, "daily_apply_cap"))
        auto_submit = bool(get_setting(session, "auto_submit")) and not review_only
        wait_minutes = float(get_setting(session, "review_wait_minutes"))
        budget = cap - applied_today(session)
        jobs = select_jobs(session, job_id, threshold)
        rec.log(f"Auto-submit {'ON' if auto_submit else 'OFF'} · threshold {threshold:g} · today {cap - budget}/{cap} applied · "
                f"{len(jobs)} job(s) queued" + (f" · stop after {limit} filled form(s)" if limit else ""))
        filled = 0
        if budget <= 0 or not jobs:
            rec.log("Nothing to do (daily cap reached or no docs_ready LinkedIn/Naukri jobs).")
            return
        resume = load_resume()
        if drafter_factory is None:
            def drafter_factory(job: Job):
                return make_drafter(lambda: GeminiClient(on_retry=rec.log), session, resume, job, rec.log)

        with browser_factory() as browser:
            signed_out = {src for src in {job.source for job in jobs} if not is_signed_in(browser, src)}
            for src in sorted(signed_out):
                rec.log(f"Not signed in to {src} in the agent profile; skipping its jobs. "
                        f"In E:\\job_search_agent run: {LOGIN_HINT.get(src, '')} (you type the password).")
            jobs = [job for job in jobs if job.source not in signed_out]
            if not jobs:
                raise RuntimeError(f"Not signed in to {', '.join(sorted(signed_out))} in the agent profile. In "
                                   f"E:\\job_search_agent run: {' / '.join(LOGIN_HINT.get(s, '') for s in sorted(signed_out))}")
            for index, job in enumerate(jobs):
                if budget <= 0:
                    rec.log(f"Daily cap of {cap} reached; the rest wait for tomorrow")
                    break
                if limit and filled >= limit:
                    rec.log(f"Reached the limit of {limit} filled form(s) for this run")
                    break
                if index:
                    rec.log(f"Pausing {pacing.between_jobs():.0f}s before the next job")
                rec.processed += 1
                try:
                    if job.source == "naukri":
                        outcome = process_naukri_job(session, browser, job, rec, pacing, auto_submit=auto_submit,
                                                     threshold=threshold, wait_minutes=wait_minutes)
                    else:
                        outcome = process_job(session, browser, job, rec, pacing, auto_submit=auto_submit, threshold=threshold,
                                              wait_minutes=wait_minutes, drafter_factory=drafter_factory)
                except Blocked as error:
                    notify("JobPilot: blocked", f"{error}. The browser is left on that page for you.")
                    rec.log(f"BLOCKED at #{job.id} {job.company}: {error}. Stopping; the window stays open for 15 min.")
                    deadline = time.monotonic() + 15 * 60
                    while time.monotonic() < deadline and browser.pages:
                        pacing.sleep(5)
                    raise
                if outcome not in ("not_easy_apply", "external", "already_applied", "failed"):
                    filled += 1
                if outcome == "submitted":
                    rec.found += 1
                    budget -= 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job-id", type=int, help="Apply to this one job")
    parser.add_argument("--review-only", action="store_true", help="Never auto-submit in this run")
    parser.add_argument("--limit", type=int, help="Stop after this many forms were filled (non-Easy-Apply jobs don't count)")
    parser.add_argument("--run-id", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        run(job_id=args.job_id, review_only=args.review_only, limit=args.limit, run_id=args.run_id)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
