"""
Example data so every screen has something to show. All rows are flagged
is_example=1 and the companies are fictional.

    python -m jobpilot.db.seed            # add examples (skipped if already there)
    python -m jobpilot.db.seed --purge    # delete every example row
"""

from __future__ import annotations

import argparse
from datetime import timedelta

from sqlalchemy import delete, func
from sqlmodel import Session, select

from jobpilot.db.models import (
    Actor,
    AgentName,
    AgentRun,
    Answer,
    Application,
    ApplicationField,
    ApplicationState,
    FieldSource,
    Job,
    JobStatus,
    RunStatus,
    utcnow,
)
from jobpilot.db.repo import set_status, upsert_job
from jobpilot.db.session import session_scope

PIPELINE = [JobStatus.found, JobStatus.shortlisted, JobStatus.docs_ready, JobStatus.applied, JobStatus.interview, JobStatus.offer]

# title, company, source, location, exp, score (skills, exp, domain), status, days ago, skills, jd focus
EXAMPLE_JOBS = [
    ("AI Business Analyst", "Kaveri Analytics", "linkedin", "Hyderabad", (2, 4), (86, 90, 82, 84), "offer", 21,
     ["SQL", "Power BI", "LLM evaluation", "BRD", "Agile"], "own requirements for LLM-powered underwriting assistants"),
    ("AI Product Analyst", "Deccan Health AI", "naukri", "Hyderabad", (2, 5), (81, 84, 80, 76), "interview", 16,
     ["Product analytics", "SQL", "A/B testing", "Python", "Mixpanel"], "measure adoption of a clinical-notes summariser"),
    ("Business Analyst - GenAI Platforms", "Godavari Fintech", "linkedin", "Hyderabad", (3, 5), (78, 80, 74, 79), "applied", 9,
     ["Process mapping", "Jira", "Prompt design", "SQL"], "translate lending ops needs into GenAI workflow specs"),
    ("Associate Product Analyst, AI", "Charminar Labs", "naukri", "Hyderabad", (1, 3), (74, 78, 70, 72), "applied", 6,
     ["Excel", "SQL", "Stakeholder management", "Dashboards"], "support PMs shipping a retrieval-augmented search feature"),
    ("AI Business Analyst (Conversational AI)", "Tellapur Digital", "linkedin", "Hyderabad (Hybrid)", (2, 4), (72, 75, 70, 69), "applied", 0,
     ["Chatbot design", "User stories", "UAT", "Confluence"], "write intents, flows and acceptance criteria for a voice bot"),
    ("Data & AI Business Analyst", "Banjara Retail Tech", "linkedin", "Hyderabad", (2, 4), (70, 72, 68, 69), "docs_ready", 4,
     ["SQL", "Tableau", "Forecasting", "Requirements"], "scope a demand-forecasting model with merchandising teams"),
    ("Product Analyst - ML Features", "Hussain Sagar Software", "naukri", "Hyderabad", (2, 4), (68, 70, 66, 67), "docs_ready", 3,
     ["Python", "SQL", "Experimentation", "Looker"], "evaluate ranking-model experiments in a B2B marketplace"),
    ("AI Solutions Business Analyst", "Kukatpally Cloud Services", "linkedin", "Hyderabad", (3, 6), (66, 70, 60, 65), "shortlisted", 2,
     ["Solutioning", "Azure OpenAI", "BRD", "Client workshops"], "run discovery workshops for enterprise copilots"),
    ("Business Analyst - Automation & AI", "Nallamala Insurance", "naukri", "Hyderabad", (2, 4), (64, 66, 62, 63), "shortlisted", 2,
     ["RPA", "Process mining", "SQL", "Visio"], "find claims processes worth automating with document AI"),
    ("Junior AI Product Analyst", "Golconda EdTech", "linkedin", "Remote (India)", (1, 3), (62, 65, 66, 52), "shortlisted", 1,
     ["Amplitude", "SQL", "Surveys", "Roadmaps"], "track learning outcomes of an AI tutor feature"),
    ("Business Analyst", "Secunderabad Logistics", "naukri", "Hyderabad", (2, 5), (55, 58, 60, 45), "found", 1,
     ["SQL", "Excel", "Process documentation"], "document warehouse processes ahead of an ML routing pilot"),
    ("AI Analyst - Risk", "Gachibowli Capital", "linkedin", "Hyderabad", (4, 7), (48, 60, 30, 52), "found", 0,
     ["Credit risk", "Python", "Model validation"], "validate credit-risk models and document AI governance"),
    ("Product Operations Analyst", "Madhapur Mobility", "naukri", "Hyderabad", (1, 3), (44, 50, 55, 25), "found", 0,
     ["Ops metrics", "Excel", "SQL"], "keep ride-ops dashboards and SOPs current"),
    ("AI Business Analyst", "Kondapur Health Systems", "linkedin", "Bengaluru", (2, 4), (None, None, None, None), "found", 0,
     ["Requirements", "HL7", "SQL"], "gather requirements for AI triage in hospital EHR integrations"),
    ("Senior Business Analyst - AI", "Jubilee Hills Consulting", "naukri", "Hyderabad", (6, 9), (38, 55, 15, 45), "rejected", 12,
     ["Program management", "Pre-sales", "GenAI strategy"], "lead pre-sales for GenAI transformation programmes"),
]

EXAMPLE_ANSWERS = [
    ("What is your notice period?", "30 days", "notice, availability"),
    ("Are you willing to relocate?", "Yes, within India", "relocation"),
    ("How many years of experience do you have with SQL?", "3", "sql, experience"),
    ("Are you legally authorized to work in India?", "Yes", "authorization"),
    ("What is your expected CTC (in LPA)?", "Open to discuss as per industry standards", "salary"),
    ("Do you have experience with Power BI?", "Yes", "tools, bi"),
]


def make_jd(title: str, company: str, focus: str, skills: list[str], exp: tuple[int, int], location: str) -> str:
    return (
        f"{company} is hiring a {title} in {location}.\n\n"
        f"About the role\nYou will {focus}, working closely with product, engineering and business stakeholders.\n\n"
        f"Responsibilities\n- Elicit and document requirements (BRDs, user stories, acceptance criteria)\n"
        f"- Analyse data to size opportunities and measure outcomes\n"
        f"- Partner with ML engineers to define evaluation criteria for AI features\n"
        f"- Run UAT and drive adoption with end users\n\n"
        f"Requirements\n- {exp[0]}-{exp[1]} years as a business or product analyst\n"
        f"- Skills: {', '.join(skills)}\n- Clear written communication; comfort with ambiguity\n\n"
        f"(Example job generated by JobPilot's seed script.)"
    )


def seed(session: Session) -> int:
    if session.exec(select(func.count()).select_from(Job).where(Job.is_example)).one():
        return 0

    now = utcnow()
    jobs: dict[str, Job] = {}
    for i, (title, company, source, location, exp, scores, status, days, skills, focus) in enumerate(EXAMPLE_JOBS, 1):
        score, s_skills, s_exp, s_domain = scores
        job, _ = upsert_job(session, {
            "title": title, "company": company, "source": source, "location": location,
            "url": f"https://example.com/jobpilot/{source}/{i:02d}",
            "exp_min": exp[0], "exp_max": exp[1],
            "jd_text": make_jd(title, company, focus, skills, exp, location),
            "skills": skills, "score": score, "score_skills": s_skills,
            "score_experience": s_exp, "score_domain": s_domain,
            "score_reason": (f"Strong overlap on {', '.join(skills[:2])}; experience band {exp[0]}-{exp[1]} yrs."
                             if score is not None else ""),
            "legacy_score": None if score is None else round(score * 0.9, 1),
            "found_at": now - timedelta(days=days, hours=i),
            "is_example": True,
        })
        # Walk the job through the pipeline so history (and the funnel) are realistic.
        target = JobStatus(status)
        path = [JobStatus.rejected] if target == JobStatus.rejected else PIPELINE[1 : PIPELINE.index(target) + 1]
        for step in path:
            set_status(session, job, step, actor=Actor.agent if step in (JobStatus.shortlisted, JobStatus.docs_ready) else Actor.user)
        if target in (JobStatus.applied, JobStatus.interview, JobStatus.offer):
            # "applied today" must stay today in IST even just after midnight, so no hour offset for it
            job.applied_at = now - (timedelta(days=days - 1, hours=1) if days > 1 else timedelta(minutes=1))
        jobs[company] = job

    for question, answer, tags in EXAMPLE_ANSWERS:
        session.add(Answer(question=question, answer=answer, tags=tags, times_used=len(tags) % 4, is_example=True))

    session.add_all([
        AgentRun(agent=AgentName.search, started_at=now - timedelta(hours=4, minutes=3), finished_at=now - timedelta(hours=4),
                 found=15, processed=15, status=RunStatus.ok, log="Imported 15 jobs (15 new, 0 updated).", is_example=True),
        AgentRun(agent=AgentName.score, started_at=now - timedelta(hours=3, minutes=50), finished_at=now - timedelta(hours=3, minutes=46),
                 found=14, processed=14, status=RunStatus.ok, log="Scored 14 jobs; 3 crossed threshold 60.", is_example=True),
        AgentRun(agent=AgentName.docs, started_at=now - timedelta(hours=3), finished_at=now - timedelta(hours=2, minutes=58),
                 found=3, processed=2, status=RunStatus.error, error="Gemini 429: quota exhausted; 1 job deferred",
                 log="Generated 2 of 3; stopped after 429 backoff.", is_example=True),
    ])

    pending = Application(job_id=jobs["Banjara Retail Tech"].id, state=ApplicationState.pending_review,
                          created_at=now - timedelta(minutes=25), is_example=True)
    blocked = Application(job_id=jobs["Hussain Sagar Software"].id, state=ApplicationState.blocked,
                          created_at=now - timedelta(hours=1), is_example=True)
    session.add_all([pending, blocked])
    session.flush()
    session.add_all([
        ApplicationField(application_id=pending.id, step=1, field_name="Mobile phone number", value_used="(from resume)", source=FieldSource.resume),
        ApplicationField(application_id=pending.id, step=2, field_name="Notice period", value_used="30 days", source=FieldSource.answer_bank),
        ApplicationField(application_id=pending.id, step=2, field_name="Years of experience with Tableau",
                         value_used="2", source=FieldSource.llm, needs_review=True),
        ApplicationField(application_id=pending.id, step=3, field_name="Why are you interested in this role?",
                         value_used="I enjoy turning forecasting needs into clear requirements...", source=FieldSource.llm, needs_review=True),
    ])
    return len(EXAMPLE_JOBS)


def purge(session: Session) -> dict[str, int]:
    """Delete every example row. History and application fields cascade."""
    counts = {}
    for model in (Application, AgentRun, Answer, Job):
        result = session.exec(delete(model).where(model.is_example))  # type: ignore[call-overload]
        counts[model.__tablename__] = result.rowcount
    return counts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Add or remove JobPilot example data.")
    parser.add_argument("--purge", action="store_true", help="Delete all rows flagged is_example")
    args = parser.parse_args(argv)
    with session_scope() as session:
        if args.purge:
            print("Purged example rows:", purge(session))
        else:
            added = seed(session)
            print(f"Seeded {added} example jobs." if added else "Example data already present; nothing added.")


if __name__ == "__main__":
    main()
