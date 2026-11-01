"""Role fit: title first, total experience second, description/skills decide similar titles ("You might like")."""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from fastapi.testclient import TestClient

from jobpilot.agents import documents, shortlist
from jobpilot.api.main import create_app
from jobpilot.agents.apply import runner
from jobpilot.db.models import Actor, Job, JobStatus
from jobpilot.db.repo import put_setting, set_status, upsert_job
from jobpilot.db.session import get_engine
from jobpilot.scoring.fit import FitConfig, classify, experience_fit, group_of, load_fit, title_fit
from jobpilot.scoring.gates import GateConfig, check_gates


@pytest.mark.parametrize("title,expected", [
    ("Business Analyst", "exact"), ("Senior Business Analyst - Risk", "exact"), ("AI Product Analyst", "exact"),
    ("Product Owner III", "exact"), ("Associate Product Manager", "exact"), ("Walk-in || Business Analyst", "exact"),
    ("Data Analyst", "similar"), ("Product Manager", "similar"), ("Senior Implementation Consultant", "similar"),
    ("Data Analysis - SQL + Core Domain Data skills", "similar"), ("Consulting Analyst", "similar"),
    ("Java Developer", "other"), ("QA Automation Engineer", "other"), ("Digital Marketing Specialist", "other"),
    ("Management Trainee (BBA/MBA)", "other"), ("AI Automation Intern", "other"), ("Data Annotation Analyst", "other"),
    ("Business Analyst Training", "other"), ("Data Analytic | Fresher | Business Analytics", "other"),
    ("Business Analyst (Entry-level Engineering Graduates)", "other"), ("Audit and Assurance Associate", "other"),
])
def test_title_fit(title, expected):
    assert title_fit(title) == expected


@pytest.mark.parametrize("low,high,expected", [
    (3, 5, "in"), (2, 6, "in"), (5, 10, "in"), (6, None, "in"), (0, 2, "in"), (None, None, "unknown"),
    (7, 12, "out"), (8, None, "out"), (0, 1, "out"), (10, 15, "out"),
])
def test_experience_is_checked_against_two_to_six_years(low, high, expected):
    assert experience_fit(low, high, FitConfig()) == expected


def fit(title, exp=(3, 5), score=75, skills=70, threshold=60):
    return classify(title, exp[0], exp[1], score, skills, threshold, FitConfig())


def test_exact_title_in_range_is_a_best_match():
    assert fit("Business Analyst").group == "best"


def test_exact_title_under_threshold_stays_below_and_unscored_is_pending():
    assert fit("Business Analyst", score=50).group == "below"
    assert fit("Business Analyst", score=None, skills=None).group == "pending"


def test_experience_outside_the_range_eliminates_even_an_exact_title():
    result = fit("Business Analyst", exp=(8, 12))
    assert result.group == "eliminate" and "outside 2-6" in result.reason


def test_similar_title_is_you_might_like_only_when_skills_and_score_fit():
    assert fit("Data Analyst", skills=70).group == "maybe"
    assert fit("Data Analyst", skills=50).group == "eliminate"            # description/skills do not fit
    assert fit("Data Analyst", score=55, skills=70).group == "eliminate"  # similar title, scored under the threshold
    assert fit("Data Analyst", score=None, skills=None).group == "pending"


def test_unknown_experience_is_kept():
    assert fit("Business Analyst", exp=(None, None)).group == "best"
    assert fit("Data Analyst", exp=(None, None)).group == "maybe"


def test_other_titles_are_eliminated():
    assert fit("Java Developer").group == "eliminate"


def test_title_gate_is_off_unless_the_setting_is_on():
    assert check_gates("Hyderabad", 3, 5, title="Brand Strategist").passed
    off = GateConfig(require_title_fit=True)
    assert not check_gates("Hyderabad", 3, 5, off, title="Java Developer").passed
    assert check_gates("Hyderabad", 3, 5, off, title="Business Analyst").passed
    assert check_gates("Hyderabad", 3, 5, off, title="Data Analyst").passed


# ---- end to end ---------------------------------------------------------------------------------------------

AI = {"score": 78, "skills": 80, "experience": 75, "domain": 80, "ai_tier": "core", "reason": "Fits.",
      "matched_skills": ["SQL"], "missing_skills": []}
JD = "We need a business analyst to own requirements, write BRDs and analyse data in SQL. " * 4
_n = iter(range(430000001, 430009999))


def add(db_url, title, **kw) -> int:
    data = {"title": title, "company": f"Co{next(_n)}", "source": "naukri", "location": "Hyderabad",
            "url": f"https://www.naukri.com/job-listings-x-{next(_n)}", "jd_text": JD, "exp_min": 3, "exp_max": 5}
    data.update(kw)
    with Session(get_engine(db_url)) as s:
        job, _ = upsert_job(s, data)
        s.commit()
        return job.id


def turn_on(db_url):
    with Session(get_engine(db_url)) as s:
        put_setting(s, "scoring", {"exp_min": 2, "exp_max": 6, "require_title_fit": True, "min_skills": 60})
        s.commit()


class Client:
    def __init__(self, response):
        self.response = response

    def generate_json(self, system, prompt):
        return dict(self.response)


@pytest.fixture(autouse=True)
def resume(tmp_path, monkeypatch):
    from jobpilot.resume import Resume

    path = tmp_path / "master_resume.md"
    path.write_text("KRISHNA M\nBusiness Analyst. SQL, Power BI, BRDs. " * 8, encoding="utf-8")
    value = Resume(path=path, text=path.read_text(encoding="utf-8"))
    monkeypatch.setattr(shortlist, "load_resume", lambda: value)


@pytest.fixture
def client(db_url):
    return TestClient(create_app(db_url))


def status_of(db_url, ident):
    with Session(get_engine(db_url)) as s:
        job = s.get(Job, ident)
        return job.status, job.notes


def test_scoring_rejects_other_titles_before_gemini_and_eliminates_poor_similar_fits(db_url):
    turn_on(db_url)
    other = add(db_url, "Java Developer")
    similar_bad = add(db_url, "Data Analyst")
    exact = add(db_url, "Business Analyst")
    shortlist.run(db_url=db_url, client=Client({**AI, "skills": 40}))      # skills sub-score 40
    assert status_of(db_url, other)[0] == JobStatus.rejected and "off-profile role (title)" in status_of(db_url, other)[1]
    state, note = status_of(db_url, similar_bad)
    assert state == JobStatus.rejected and "unsuitable (similar title, skills fit only 40" in note
    assert status_of(db_url, exact)[0] == JobStatus.shortlisted            # exact titles are not judged on the skills sub-score


def test_similar_title_with_a_good_fit_is_shortlisted_as_you_might_like(db_url):
    turn_on(db_url)
    similar = add(db_url, "Data Analyst")
    shortlist.run(db_url=db_url, client=Client(AI))
    assert status_of(db_url, similar)[0] == JobStatus.shortlisted
    with Session(get_engine(db_url)) as s:
        assert group_of(s.get(Job, similar), load_fit(s)).group == "maybe"


def test_documents_and_apply_queues_take_best_matches_only(db_url):
    turn_on(db_url)
    best = add(db_url, "Business Analyst")
    maybe = add(db_url, "Data Analyst")
    shortlist.run(db_url=db_url, client=Client(AI))
    with Session(get_engine(db_url)) as s:
        assert [j.id for j in documents.select_jobs(s, 10, None)] == [best]
        for ident in (best, maybe):
            set_status(s, s.get(Job, ident), JobStatus.docs_ready, actor=Actor.agent)
        s.commit()
        assert [j.id for j in runner.select_jobs(s, None, 60.0)] == [best]


def test_api_lists_the_group_and_filters_by_it(client, db_url):
    turn_on(db_url)
    best = add(db_url, "Business Analyst")
    maybe = add(db_url, "Data Analyst")
    shortlist.run(db_url=db_url, client=Client(AI))
    body = client.get("/api/jobs", params={"include_examples": False}).json()
    groups = {j["id"]: j["match_group"] for j in body["items"]}
    assert groups[best] == "best" and groups[maybe] == "maybe"
    only = client.get("/api/jobs", params={"match": "maybe", "include_examples": False}).json()
    assert [j["id"] for j in only["items"]] == [maybe] and only["total"] == 1
