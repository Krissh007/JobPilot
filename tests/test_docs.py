"""Docs agent: real legacy tailoring code, Gemini faked. Focus: nothing untrue reaches the documents."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from docx import Document
from sqlmodel import Session, select

from jobpilot.agents import docs
from jobpilot.db.models import Actor, AgentRun, Job, JobStatus, RunStatus, StatusHistory
from jobpilot.db.repo import set_status, upsert_job
from jobpilot.db.session import get_engine
from jobpilot.resume import Resume

MASTER = """KRISHNA TEST
AI Business Analyst
PROFESSIONAL EXPERIENCE
LOGNORMAL ANALYTICS PVT. LTD.
Business Analyst | AI Product Analyst
Hyderabad, India | Oct 2025 – Sep 2026
- Gathered requirements for LLM features with 12 stakeholders using SQL and Power BI
SOFTIC DIGITECH PVT. LTD.
Business Analyst
Hyderabad, India | Feb 2024 – Sep 2025
- Wrote BRDs and user stories in Jira for an analytics platform
EDUCATION
Bachelor of Engineering, Electronics & Communication Engineering
Gandhi Institute of Technology and Management, India
SKILLS
SQL, Power BI, Jira, Requirements Gathering, User Stories, Prompt Engineering
"""

GOOD = {
    "candidate": {"name": "Krishna Test", "contact_line": ""},
    "headline": "AI Business Analyst",
    "summary": "Business analyst who gathers requirements for LLM features.",
    "skills": ["SQL", "Power BI", "Requirements Gathering", "Kubernetes"],  # Kubernetes is NOT in the resume
    "experience": [
        {"title": "Business Analyst | AI Product Analyst", "company": "LogNormal Analytics Pvt. Ltd.", "location": "Hyderabad",
         "dates": "Oct 2025 – Sep 2026", "bullets": ["Gathered LLM requirements with 12 stakeholders using SQL", "Cut costs by 40%"]},
        {"title": "Business Analyst", "company": "Softic Digitech Pvt. Ltd.", "location": "Hyderabad",
         "dates": "Feb 2024 – Sep 2025", "bullets": ["Wrote BRDs and user stories in Jira"]},
    ],
    "projects": [],
    "education": [{"degree": "Bachelor of Engineering, Electronics & Communication Engineering",
                   "institution": "Gandhi Institute of Technology and Management", "dates": "", "details": ""}],
    "certifications": [],
    "cover_letter": {"salutation": "Dear Hiring Manager,", "paragraphs": ["I gather LLM requirements.", "I write BRDs in Jira."], "closing": "Sincerely,"},
    "jd_keywords": ["SQL", "LLM", "Tableau"],
    "missing_keywords": ["Tableau"],
}


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), 0

    def generate_json(self, system, prompt):
        self.calls += 1
        assert "MASTER RESUME" in prompt and "only source of facts" in prompt  # legacy truth-first prompt is used
        return copy.deepcopy(self.responses.pop(0))


@pytest.fixture
def setup(db_url, tmp_path, monkeypatch):
    resume = Resume(path=tmp_path / "Krishna_Resume.docx", text=MASTER)
    monkeypatch.setattr(docs, "load_resume", lambda: resume)
    monkeypatch.setattr(docs, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(docs, "contact_details", lambda: {"full_name": "Krishna Test", "email": "k@example.com", "phone": "000"})
    with Session(get_engine(db_url)) as s:
        job, _ = upsert_job(s, {"title": "AI Business Analyst", "company": "Acme AI", "source": "linkedin",
                                "url": "https://www.linkedin.com/jobs/view/4200000001", "location": "Hyderabad",
                                "jd_text": "We need an AI Business Analyst for LLM products. SQL, Tableau, stakeholder management. " * 6,
                                "score": 88})
        set_status(s, job, JobStatus.shortlisted, actor=Actor.agent)
        s.commit()
        return job.id


def get_job(db_url, job_id) -> Job:
    with Session(get_engine(db_url)) as s:
        return s.get(Job, job_id)


def docx_text(path: str) -> str:
    return "\n".join(p.text for p in Document(path).paragraphs)


def test_generates_truthful_documents_and_moves_to_docs_ready(db_url, setup):
    docs.run(db_url=db_url, client=FakeClient(GOOD))
    job = get_job(db_url, setup)
    assert job.status == JobStatus.docs_ready
    resume_text, letter_text = docx_text(job.resume_path), docx_text(job.cover_letter_path)
    assert Path(job.resume_path).parent.name == f"Acme_AI_{setup}"
    assert "Oct 2025 – Sep 2026" in resume_text and "LogNormal Analytics" in resume_text
    assert "Kubernetes" not in resume_text  # legacy fact check removed the unsupported skill
    assert "40%" not in resume_text  # invented number removed
    assert "Krishna Test" in letter_text and "Acme AI" in letter_text
    report = (Path(job.resume_path).parent / "match_report.md").read_text(encoding="utf-8")
    assert "Removed unsupported skill: Kubernetes" in report and "Tableau" in report
    with Session(get_engine(db_url)) as s:
        last = s.exec(select(StatusHistory).where(StatusHistory.job_id == setup).order_by(StatusHistory.id.desc())).first()
        run = s.exec(select(AgentRun)).one()
    assert (last.to_status, last.actor) == (JobStatus.docs_ready, Actor.agent)
    assert run.status == RunStatus.ok and (run.found, run.processed) == (1, 1)


def test_changed_dates_are_corrected_from_master(db_url, setup):
    altered = copy.deepcopy(GOOD)
    altered["experience"][0]["dates"] = "Jun 2024 – Present"  # the old, wrong dates
    docs.run(db_url=db_url, client=FakeClient(altered))
    job = get_job(db_url, setup)
    text = docx_text(job.resume_path)
    assert "Oct 2025 – Sep 2026" in text and "Jun 2024" not in text
    report = (Path(job.resume_path).parent / "match_report.md").read_text(encoding="utf-8")
    assert "Corrected dates for LogNormal Analytics Pvt. Ltd.: 'Jun 2024 – Present' -> 'Oct 2025 – Sep 2026'" in report


def test_unknown_title_writes_nothing(db_url, setup, tmp_path):
    invented = copy.deepcopy(GOOD)
    invented["experience"][0]["title"] = "Head of AI Strategy"
    docs.run(db_url=db_url, client=FakeClient(invented))
    job = get_job(db_url, setup)
    assert job.status == JobStatus.shortlisted and not job.resume_path
    assert not (tmp_path / "output").exists() or not list((tmp_path / "output").rglob("*.docx"))
    with Session(get_engine(db_url)) as s:
        assert "SKIPPED, Title 'Head of AI Strategy'" in s.exec(select(AgentRun)).one().log


def test_regeneration_uses_cache(db_url, setup):
    docs.run(db_url=db_url, client=FakeClient(GOOD))
    client = FakeClient()  # would fail if called
    docs.run(job_id=setup, db_url=db_url, client=client)
    assert client.calls == 0
    with Session(get_engine(db_url)) as s:
        assert "[cache]" in s.exec(select(AgentRun).order_by(AgentRun.id.desc())).first().log


def test_master_dates_lookup_handles_company_variants():
    assert docs.master_dates_for("LogNormal Analytics Pvt. Ltd.", MASTER) == ["Oct 2025 – Sep 2026"]
    assert docs.master_dates_for("Softic Digitech", MASTER) == ["Feb 2024 – Sep 2025"]
    assert docs.master_dates_for("Unknown Corp", MASTER) == []


def test_examples_and_unshortlisted_jobs_are_not_selected(db_url, setup):
    with Session(get_engine(db_url)) as s:
        upsert_job(s, {"title": "Ex", "company": "Example Co", "url": "https://example.com/x", "jd_text": "x" * 400, "is_example": True})
        s.commit()
        assert [j.id for j in docs.select_jobs(s, 10, None)] == [setup]


def test_bad_json_retried_once_and_long_titles_ok(db_url, setup):
    from jobpilot.llm.gemini import LLMError

    class Flaky(FakeClient):
        def generate_json(self, system, prompt):
            self.calls += 1
            if self.calls == 1:
                raise LLMError("Model did not return valid JSON: Expecting value")
            assert "not valid JSON" in prompt
            return copy.deepcopy(GOOD)

    with Session(get_engine(db_url)) as s:
        job = s.get(Job, setup)
        job.title = "FY27 | USI | Audit and Assurance | " + "Associate Senior Assistant, Product Strategy & Management " * 5
        s.add(job)
        s.commit()
    client = Flaky()
    docs.run(db_url=db_url, client=client)
    assert client.calls == 2 and get_job(db_url, setup).status == JobStatus.docs_ready


def test_unexpected_error_skips_job_not_run(db_url, setup, monkeypatch):
    monkeypatch.setattr(docs, "generate_one", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    docs.run(db_url=db_url, client=FakeClient())
    with Session(get_engine(db_url)) as s:
        run = s.exec(select(AgentRun)).one()
    assert run.status == RunStatus.ok and "SKIPPED, boom" in run.log


def test_word_properties_fit_255_chars():
    from jobpilot.legacy.bridge import fit_keywords

    long = ["Business Process Analysis", "Cross-functional Collaboration", "Stakeholder Management"] * 10
    kept = fit_keywords(long)
    assert 0 < len(kept) < 25 and len(", ".join(kept)) <= 255 and kept == long[:len(kept)]
    assert fit_keywords(["SQL", "Jira"]) == ["SQL", "Jira"]
