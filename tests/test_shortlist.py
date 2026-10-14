from __future__ import annotations

import pytest
from sqlmodel import Session, select

from jobpilot.agents import shortlist
from jobpilot.db.models import Actor, AgentRun, Job, JobStatus, StatusHistory
from jobpilot.db.repo import put_setting, upsert_job
from jobpilot.db.session import get_engine
from jobpilot.llm.gemini import QuotaExhausted
from jobpilot.resume import Resume
from jobpilot.scoring.gates import GATE_PREFIX, GateConfig, check_gates, experience_gate, india_gate

AI = {"score": 78, "skills": 80, "experience": 75, "domain": 80, "ai_role": True, "reason": "Owns LLM feature requirements.",
      "matched_skills": ["SQL"], "missing_skills": []}
JD = "We need an AI Business Analyst to own requirements for LLM features, write BRDs and analyse data in SQL. " * 3


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.prompts = list(responses), []

    def generate_json(self, system, prompt):
        self.prompts.append(prompt)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def resume(tmp_path, monkeypatch) -> Resume:
    path = tmp_path / "master_resume.md"
    path.write_text("KRISHNA M\nAI Business Analyst. SQL, Power BI, LLM evaluation, BRDs. " * 5, encoding="utf-8")
    value = Resume(path=path, text=path.read_text(encoding="utf-8"))
    monkeypatch.setattr(shortlist, "load_resume", lambda: value)
    return value


_n = iter(range(150926500001, 150926509999))


def add_job(db_url, **kw) -> int:
    data = {"title": "AI Business Analyst", "company": f"Co{next(_n)}", "source": "naukri", "location": "Hyderabad",
            "url": f"https://www.naukri.com/job-listings-x-{next(_n)}", "jd_text": JD, "exp_min": 3, "exp_max": 5}
    data.update(kw)
    with Session(get_engine(db_url)) as s:
        job, _ = upsert_job(s, data)
        s.commit()
        return job.id


def get(db_url, ident) -> Job:
    with Session(get_engine(db_url)) as s:
        return s.get(Job, ident)


# ---- gates ---------------------------------------------------------------

@pytest.mark.parametrize("location,ok", [
    ("Hyderabad", True), ("Hyderabad, Telangana, India", True), ("Gurugram", True), ("Remote", True), ("", True),
    ("Pune, Bengaluru", True), ("London, United Kingdom", False), ("Dubai, UAE", False), ("Remote - USA", False),
    ("Singapore", False), ("Indianapolis, IN", False)])
def test_india_gate(location, ok):
    assert india_gate(location).passed is ok


@pytest.mark.parametrize("lo,hi,ok", [
    (3, 5, True), (None, None, True), (1, 2, False), (0, 3, True), (7, 10, True), (8, 12, False),
    (5, None, True), (9, None, False), (None, 2, False), (2, 4, True)])
def test_experience_gate_overlaps_3_to_7(lo, hi, ok):
    assert experience_gate(lo, hi, GateConfig()).passed is ok


def test_gate_range_is_configurable_and_reports_reason():
    assert check_gates("Hyderabad", 1, 2, GateConfig(exp_min=1, exp_max=3)).passed
    result = check_gates("Paris", 3, 5)
    assert not result.passed and "not India" in result.reason


# ---- agent -----------------------------------------------------------------

def test_gate_failures_rejected_without_calling_gemini(db_url):
    foreign = add_job(db_url, location="London, UK")
    senior = add_job(db_url, exp_min=10, exp_max=15)
    client = FakeClient()   # fails if called
    shortlist.run(db_url=db_url, client=client)
    for ident, text in ((foreign, "not India"), (senior, "above 3-7")):
        job = get(db_url, ident)
        assert job.status == JobStatus.rejected and text in job.notes and job.score is None
    with Session(get_engine(db_url)) as s:
        hist = s.exec(select(StatusHistory).where(StatusHistory.job_id == foreign, StatusHistory.to_status == JobStatus.rejected)).one()
        assert hist.actor == Actor.agent and hist.from_status == JobStatus.found
    assert client.prompts == []


def test_gemini_is_sole_judge_no_rule_floor_and_legacy_score_ignored(db_url):
    low_legacy = add_job(db_url, legacy_score=5)       # the old 45 floor would have dropped this
    client = FakeClient(AI)
    shortlist.run(db_url=db_url, client=client)
    job = get(db_url, low_legacy)
    assert len(client.prompts) == 1 and job.score == 78 and job.status == JobStatus.shortlisted


def test_threshold_60_boundary(db_url):
    at = add_job(db_url)
    below = add_job(db_url, jd_text=JD + " MARKER-BELOW")

    class ByJD:
        def generate_json(self, system, prompt):
            return {**AI, "score": 59 if "MARKER-BELOW" in prompt else 60}

    shortlist.run(db_url=db_url, client=ByJD())
    scores = {i: get(db_url, i) for i in (at, below)}
    assert scores[at].status == JobStatus.shortlisted
    assert scores[below].status == JobStatus.found and scores[below].score == 59   # scored, not re-scored later


def test_non_ai_role_capped_below_threshold_even_if_gemini_scores_high(db_url):
    job_id = add_job(db_url, title="Business Analyst")
    shortlist.run(db_url=db_url, client=FakeClient({**AI, "score": 92, "ai_role": False}))
    job = get(db_url, job_id)
    assert job.score == 59 and job.status == JobStatus.found and "Capped" in job.score_reason


def test_ai_role_not_capped(db_url):
    job_id = add_job(db_url)
    shortlist.run(db_url=db_url, client=FakeClient({**AI, "score": 95}))
    assert get(db_url, job_id).score == 95


def test_missing_ai_role_field_is_invalid_then_retried(db_url):
    job_id = add_job(db_url)
    bad = {k: v for k, v in AI.items() if k != "ai_role"}
    client = FakeClient(bad, AI)
    shortlist.run(db_url=db_url, client=client)
    assert len(client.prompts) == 2 and "rejected" in client.prompts[1] and get(db_url, job_id).score == 78


def test_already_scored_and_user_decided_jobs_untouched(db_url):
    scored = add_job(db_url)
    shortlist.run(db_url=db_url, client=FakeClient({**AI, "score": 40}))
    applied = add_job(db_url, location="Tokyo", status=JobStatus.applied)
    shortlist.run(db_url=db_url, client=FakeClient())
    assert get(db_url, scored).score == 40
    assert get(db_url, applied).status == JobStatus.applied


def test_jobs_without_jd_are_gated_but_wait_for_enrichment(db_url):
    thin = add_job(db_url, jd_text="short card snippet")
    foreign_thin = add_job(db_url, jd_text="", location="Berlin")
    shortlist.run(db_url=db_url, client=FakeClient())
    assert get(db_url, thin).status == JobStatus.found and get(db_url, thin).score is None
    assert get(db_url, foreign_thin).status == JobStatus.rejected
    with Session(get_engine(db_url)) as s:
        assert "1 without a usable JD" in s.exec(select(AgentRun).order_by(AgentRun.id.desc())).first().log


def test_dry_run_reports_but_writes_nothing(db_url):
    job_id = add_job(db_url, location="Paris")
    shortlist.run(db_url=db_url, dry_run=True)
    assert get(db_url, job_id).status == JobStatus.found


def test_quota_exhausted_stops_run_keeps_earlier_scores(db_url):
    a, b = add_job(db_url), add_job(db_url, jd_text=JD + " B")
    with pytest.raises(QuotaExhausted):
        shortlist.run(db_url=db_url, client=FakeClient(AI, QuotaExhausted("429")))
    scored = [get(db_url, i).score for i in (a, b)]
    assert sorted(s is None for s in scored) == [False, True]


def test_scoring_gate_range_read_from_settings(db_url):
    job_id = add_job(db_url, exp_min=1, exp_max=2)
    with Session(get_engine(db_url)) as s:
        put_setting(s, "scoring", {"exp_min": 1, "exp_max": 3})
        s.commit()
    shortlist.run(db_url=db_url, client=FakeClient(AI))
    assert get(db_url, job_id).status == JobStatus.shortlisted


def test_job_with_no_experience_on_card_passes_gate_and_goes_to_gemini(db_url):
    job_id = add_job(db_url, exp_min=None, exp_max=None, jd_text=JD)   # JD states no years either
    client = FakeClient(AI)
    shortlist.run(db_url=db_url, client=client)
    assert len(client.prompts) == 1 and get(db_url, job_id).status == JobStatus.shortlisted


def test_experience_rechecked_from_jd_after_enrichment(db_url):
    senior = add_job(db_url, exp_min=None, exp_max=None, jd_text=JD + " Requires 10-15 years of experience.")
    junior = add_job(db_url, exp_min=None, exp_max=None, jd_text=JD + " Requires 0-1 years of experience.")
    fine = add_job(db_url, exp_min=None, exp_max=None, jd_text=JD + " Requires 4-6 years of experience.")
    client = FakeClient(AI)
    shortlist.run(db_url=db_url, client=client)
    assert len(client.prompts) == 1
    for ident in (senior, junior):
        job = get(db_url, ident)
        assert job.status == JobStatus.rejected and job.notes.startswith(GATE_PREFIX)
    ok = get(db_url, fine)
    assert (ok.exp_min, ok.exp_max) == (4, 6) and ok.status == JobStatus.shortlisted


def test_gate_note_has_fixed_prefix(db_url):
    job_id = add_job(db_url, location="Paris", notes="my own note")
    shortlist.run(db_url=db_url, client=FakeClient())
    assert get(db_url, job_id).notes == "my own note\nGate: not India (Paris)"
