"""
Auto-submit readiness: the evidence for turning auto-submit ON. Read-only; it never changes a setting.

    python -m jobpilot.agents.apply.readiness            # track record + verdict
    python -m jobpilot.agents.apply.readiness --min-decisions 30 --min-approval 0.97

Every application you decided in the Review queue is one data point: approved (submitted after your
approval, or by you in the browser) or skipped_by_you. A review is CLEAN when it is the only kind
auto-submit would ever send unattended:

    - the form reached the Submit page (not "incomplete"),
    - every field came from your resume/profile or answer bank (no Gemini draft),
    - no empty required field and no consent/terms box left for you.

Verdict: clean reviews are enough (min_decisions) and you approved nearly all of them (min_approval).
Timeouts and closed tabs are not decisions; external / failed / blocked / already-applied are not reviews.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field

from sqlmodel import Session, select

from jobpilot.db.models import Actor, Application, ApplicationField, ApplicationState, JobStatus, StatusHistory
from jobpilot.db.session import get_engine

NOT_A_REVIEW = {"external", "not_easy_apply", "failed", "blocked"}
UNDECIDED = {"timeout", "tab_closed"}


@dataclass
class Case:
    app_id: int
    job_id: int
    outcome: str   # approved | skipped | undecided | pending | auto_submitted | not_a_review
    clean: bool


@dataclass
class Readiness:
    cases: list[Case] = field(default_factory=list)
    min_decisions: int = 20
    min_approval: float = 0.95

    def count(self, outcome: str, clean: bool | None = None) -> int:
        return sum(1 for c in self.cases if c.outcome == outcome and (clean is None or c.clean == clean))

    def decided(self, clean: bool | None = None) -> int:
        return self.count("approved", clean) + self.count("skipped", clean)

    def approval_rate(self, clean: bool | None = None) -> float | None:
        n = self.decided(clean)
        return self.count("approved", clean) / n if n else None

    @property
    def ready(self) -> bool:
        rate = self.approval_rate(True)
        return self.decided(True) >= self.min_decisions and rate is not None and rate >= self.min_approval

    def verdict(self) -> str:
        n, rate = self.decided(True), self.approval_rate(True)
        if self.ready:
            return (f"READY to consider turning auto-submit ON: {n} clean reviews, {rate:.0%} approved "
                    f"(needs {self.min_decisions}+ at {self.min_approval:.0%}+). Auto-submit would still only send clean cases.")
        if n < self.min_decisions:
            return f"NOT YET: {n}/{self.min_decisions} clean reviews decided. Keep reviewing."
        return f"NOT YET: clean-case approval is {rate:.0%}, below {self.min_approval:.0%}. Look at why clean cases were skipped."

    def report(self) -> str:
        def line(label: str, clean: bool | None) -> str:
            rate = self.approval_rate(clean)
            return (f"{label:<22} approved {self.count('approved', clean):>3} | skipped {self.count('skipped', clean):>3} | "
                    f"approval {'n/a' if rate is None else f'{rate:.0%}'}")

        return "\n".join([
            line("All reviews", None),
            line("Clean reviews", True),
            line("Needed attention", False),
            f"Undecided (timeout / tab closed): {self.count('undecided')} | waiting now: {self.count('pending')} | "
            f"auto-submitted: {self.count('auto_submitted')} | not reviews (external/failed/blocked): {self.count('not_a_review')}",
            self.verdict(),
        ])


def classify(app: Application, fields: list[ApplicationField], applied_by: Actor | None) -> Case:
    clean = bool(fields) and app.reason != "incomplete" and not any(f.needs_review for f in fields)
    base = dict(app_id=app.id, job_id=app.job_id, clean=clean)
    if app.state == ApplicationState.submitted:
        if applied_by == Actor.user:
            return Case(outcome="approved", **base)
        # By the agent: auto-submit if it filled fields, otherwise "LinkedIn says already applied".
        return Case(outcome="auto_submitted" if fields else "not_a_review", **base)
    if app.state == ApplicationState.approved:
        return Case(outcome="approved", **base)
    if app.state == ApplicationState.skipped:
        if app.reason == "skipped_by_you":
            return Case(outcome="skipped", **base)
        return Case(outcome="undecided" if app.reason in UNDECIDED else "not_a_review", **base)
    if app.state == ApplicationState.pending_review:
        return Case(outcome="pending", **base)
    return Case(outcome="not_a_review", **base)  # blocked, filling


def compute(session: Session, min_decisions: int = 20, min_approval: float = 0.95) -> Readiness:
    fields: dict[int, list[ApplicationField]] = defaultdict(list)
    for f in session.exec(select(ApplicationField)):
        fields[f.application_id].append(f)
    applied_by: dict[int, Actor] = {}
    for h in session.exec(select(StatusHistory).where(StatusHistory.to_status == JobStatus.applied).order_by(StatusHistory.id)):
        applied_by[h.job_id] = h.actor
    apps = session.exec(select(Application).where(Application.is_example.is_(False))).all()
    return Readiness([classify(a, fields[a.id], applied_by.get(a.job_id)) for a in apps], min_decisions, min_approval)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-decisions", type=int, default=20)
    parser.add_argument("--min-approval", type=float, default=0.95)
    args = parser.parse_args(argv)
    with Session(get_engine()) as session:
        print(compute(session, args.min_decisions, args.min_approval).report())


if __name__ == "__main__":
    main()
