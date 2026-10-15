"""Agent 3 (Document Generation): real legacy tailoring code, Gemini faked."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from docx import Document
from sqlmodel import Session, select

from jobpilot.agents import documents
from jobpilot.tailoring import checks
from jobpilot.db.models import Actor, AgentRun, Job, JobStatus
from jobpilot.db.repo import set_status, upsert_job
from jobpilot.db.session import get_engine
from jobpilot.resume import Resume
from tests.doc_fixtures import GOOD as OLD_GOOD
from tests.doc_fixtures import MASTER, FakeClient

LETTER = ("I gather requirements for LLM features and work with twelve stakeholders using SQL and Power BI every week. "
          "That work taught me to turn ambiguous product ideas into clear, testable requirements that engineers can build. ")
GOOD = copy.deepcopy(OLD_GOOD)
GOOD["cover_letter"]["paragraphs"] = [LETTER * 2, LETTER * 2, "I write BRDs and user stories in Jira. " * 4]


@pytest.fixture
def setup(db_url, tmp_path, monkeypatch):
    resume = Resume(path=tmp_path / "Krishna_Resume.docx", text=MASTER)
    monkeypatch.setattr(documents, "load_resume", lambda: resume)
    monkeypatch.setattr(checks, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(checks, "contact_details", lambda: {"full_name": "Krishna Test", "email": "k@example.com", "phone": "000"})
    return db_url


def add_shortlisted(db_url, title="AI Business Analyst", company="Acme AI", n=1) -> int:
    with Session(get_engine(db_url)) as s:
        job, _ = upsert_job(s, {"title": title, "company": company, "source": "linkedin", "location": "Hyderabad",
                                "url": f"https://www.linkedin.com/jobs/view/430000000{n}", "score": 88,
                                "jd_text": "We need an AI Business Analyst for LLM products. SQL, Tableau, stakeholders. " * 6})
        set_status(s, job, JobStatus.shortlisted, actor=Actor.agent)
        s.commit()
        return job.id


def get_job(db_url, ident) -> Job:
    with Session(get_engine(db_url)) as s:
        return s.get(Job, ident)


def run_log(db_url) -> str:
    with Session(get_engine(db_url)) as s:
        return s.exec(select(AgentRun).order_by(AgentRun.id.desc())).first().log


def text_of(path: str) -> str:
    return "\n".join(p.text for p in Document(path).paragraphs)


def test_generates_truthful_documents_and_moves_to_docs_ready(setup):
    job_id = add_shortlisted(setup)
    documents.run(db_url=setup, client=FakeClient(GOOD))
    job = get_job(setup, job_id)
    assert job.status == JobStatus.docs_ready
    resume_text = text_of(job.resume_path)
    assert "Kubernetes" not in resume_text and "40%" not in resume_text      # fact check still strips both
    assert "Oct 2025 – Sep 2026" in resume_text
    assert "Acme AI" in text_of(job.cover_letter_path)


@pytest.mark.parametrize("title", ["", "(untitled)", "Unknown", "N/A"])
def test_unknown_job_title_is_skipped_without_calling_gemini(setup, title):
    job_id = add_shortlisted(setup, title=title)
    client = FakeClient()   # fails if called
    documents.run(db_url=setup, client=client)
    job = get_job(setup, job_id)
    assert job.status == JobStatus.shortlisted and not job.resume_path and client.calls == 0
    assert "SKIPPED, Job title is unknown" in run_log(setup)


def test_job_title_known():
    assert documents.job_title_known("Insights Analyst") and not documents.job_title_known("  (Untitled) ")


def test_short_cover_letter_skipped_and_writes_nothing(setup, tmp_path):
    job_id = add_shortlisted(setup)
    documents.run(db_url=setup, client=FakeClient(OLD_GOOD))    # two one-line paragraphs
    assert get_job(setup, job_id).status == JobStatus.shortlisted
    assert "Cover letter too short" in run_log(setup)
    assert not (tmp_path / "output").exists() or not list((tmp_path / "output").rglob("*.docx"))


def test_single_paragraph_letter_skipped(setup):
    job_id = add_shortlisted(setup)
    one = copy.deepcopy(GOOD)
    one["cover_letter"]["paragraphs"] = [LETTER * 10]
    documents.run(db_url=setup, client=FakeClient(one))
    assert get_job(setup, job_id).status == JobStatus.shortlisted and "1 paragraph(s)" in run_log(setup)


def test_registered_alternative_title_is_accepted_and_noted(setup):
    job_id = add_shortlisted(setup)
    alias = copy.deepcopy(GOOD)
    alias["experience"][1]["title"] = "Data Analyst"          # registered, but not in this master resume
    documents.run(db_url=setup, client=FakeClient(alias))
    job = get_job(setup, job_id)
    assert job.status == JobStatus.docs_ready
    report = (Path(job.resume_path).parent / "match_report.md").read_text(encoding="utf-8")
    assert "Title 'Data Analyst' at Softic Digitech Pvt. Ltd. accepted as a registered alternative title" in report


def test_unregistered_invented_title_still_skips(setup):
    job_id = add_shortlisted(setup)
    invented = copy.deepcopy(GOOD)
    invented["experience"][0]["title"] = "Head of AI Strategy"
    documents.run(db_url=setup, client=FakeClient(invented))
    assert get_job(setup, job_id).status == JobStatus.shortlisted
    assert "Title 'Head of AI Strategy'" in run_log(setup)


def test_is_registered_title_is_whole_title_only():
    assert documents.is_registered_title("Insights Analyst")
    assert documents.is_registered_title("Business Analyst | AI Product Analyst")
    assert not documents.is_registered_title("Senior Data Analyst")
    assert not documents.is_registered_title("Business Analyst | Head of Sales")
    assert not documents.is_registered_title("")


def test_wrong_dates_still_corrected(setup):
    job_id = add_shortlisted(setup)
    altered = copy.deepcopy(GOOD)
    altered["experience"][0]["dates"] = "Jun 2024 – Present"
    documents.run(db_url=setup, client=FakeClient(altered))
    text = text_of(get_job(setup, job_id).resume_path)
    assert "Oct 2025 – Sep 2026" in text and "Jun 2024" not in text


def test_one_bad_job_does_not_stop_the_run(setup):
    bad = add_shortlisted(setup, title="", n=1)
    good = add_shortlisted(setup, company="Beta AI", n=2)
    documents.run(db_url=setup, client=FakeClient(GOOD))
    assert get_job(setup, bad).status == JobStatus.shortlisted and get_job(setup, good).status == JobStatus.docs_ready
