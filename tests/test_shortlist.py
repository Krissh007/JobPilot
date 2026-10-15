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

AI = {"score": 78, "skills": 80, "experience": 75, "domain": 80, "ai_tier": "core", "reason": "Owns LLM feature requirements.",
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


def test_non_ai_role_capped_at_65_even_if_gemini_scores_high(db_url):
    job_id = add_job(db_url, title="Business Analyst")
    shortlist.run(db_url=db_url, client=FakeClient({**AI, "score": 92, "ai_tier": "non_ai"}))
    job = get(db_url, job_id)
    assert job.score == 65 and job.status == JobStatus.shortlisted and "Capped" in job.score_reason


def test_ai_core_not_capped(db_url):
    job_id = add_job(db_url)
    shortlist.run(db_url=db_url, client=FakeClient({**AI, "score": 95}))
    assert get(db_url, job_id).score == 95


def test_missing_ai_tier_field_is_invalid_then_retried(db_url):
    job_id = add_job(db_url)
    bad = {k: v for k, v in AI.items() if k != "ai_tier"}
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


# ---- three AI tiers --------------------------------------------------------------

@pytest.mark.parametrize("raw,label", [("core", "AI-core"), ("adjacent", "AI-adjacent"), ("non_ai", "non-AI"),
                                       ("AI-adjacent", "AI-adjacent"), ("Non AI", "non-AI"), ("ai_core", "AI-core")])
def test_tier_is_stored_and_readable(db_url, raw, label):
    job_id = add_job(db_url)
    shortlist.run(db_url=db_url, client=FakeClient({**AI, "ai_tier": raw}))
    job = get_job_row(db_url, job_id)
    assert shortlist.tier_of(job) == label and job.score_reason.endswith(f"[Tier: {label}]")


def get_job_row(db_url, ident):
    return get(db_url, ident)


def new_prompt(db_url):
    """What a prompt-version bump does: earlier Gemini answers are no longer reused."""
    from jobpilot.db.models import LLMCache

    with Session(get_engine(db_url)) as s:
        for row in s.exec(select(LLMCache)):
            s.delete(row)
        s.commit()


def test_adjacent_is_not_capped_but_non_ai_is(db_url):
    adjacent = add_job(db_url, jd_text=JD + " ADJ")
    non_ai = add_job(db_url, jd_text=JD + " NON")

    class ByJD:
        def generate_json(self, system, prompt):
            return {**AI, "score": 90, "ai_tier": "adjacent" if " ADJ" in prompt else "non_ai"}

    shortlist.run(db_url=db_url, client=ByJD())
    a, n = get(db_url, adjacent), get(db_url, non_ai)
    assert (a.score, a.status) == (90, JobStatus.shortlisted)
    assert (n.score, n.status) == (65, JobStatus.shortlisted) and "[Capped: non-AI]" in n.score_reason


def test_unknown_tier_value_is_invalid_then_retried(db_url):
    job_id = add_job(db_url)
    client = FakeClient({**AI, "ai_tier": "maybe"}, {**AI, "ai_tier": "adjacent"})
    shortlist.run(db_url=db_url, client=client)
    assert len(client.prompts) == 2 and shortlist.tier_of(get(db_url, job_id)) == "AI-adjacent"


def test_old_prompt_cache_is_not_reused_after_the_tier_change():
    assert shortlist.PROMPT_VERSION == "shortlist:v2"


def test_rescore_by_ids_updates_tier_and_can_promote(db_url):
    job_id = add_job(db_url)
    shortlist.run(db_url=db_url, client=FakeClient({**AI, "score": 55, "ai_tier": "non_ai"}))
    assert get(db_url, job_id).status == JobStatus.found
    new_prompt(db_url)
    shortlist.run(db_url=db_url, job_ids=[job_id], client=FakeClient({**AI, "score": 80, "ai_tier": "adjacent"}))
    job = get(db_url, job_id)
    assert job.score == 80 and job.status == JobStatus.shortlisted and shortlist.tier_of(job) == "AI-adjacent"


def test_rescore_demotes_a_shortlisted_job_that_no_longer_qualifies(db_url):
    job_id = add_job(db_url)
    shortlist.run(db_url=db_url, client=FakeClient(AI))
    assert get(db_url, job_id).status == JobStatus.shortlisted
    new_prompt(db_url)
    shortlist.run(db_url=db_url, job_ids=[job_id], client=FakeClient({**AI, "score": 55, "ai_tier": "non_ai"}))
    job = get(db_url, job_id)
    assert job.status == JobStatus.found and job.score == 55
    with Session(get_engine(db_url)) as s:
        last = s.exec(select(StatusHistory).where(StatusHistory.job_id == job_id).order_by(StatusHistory.id.desc())).first()
    assert (last.from_status, last.to_status, last.actor) == (JobStatus.shortlisted, JobStatus.found, Actor.agent)


def test_rescore_never_demotes_once_documents_exist(db_url):
    job_id = add_job(db_url)
    shortlist.run(db_url=db_url, client=FakeClient(AI))
    with Session(get_engine(db_url)) as s:
        job = s.get(Job, job_id)
        job.resume_path = "C:/x/resume.docx"
        s.add(job)
        s.commit()
    shortlist.run(db_url=db_url, job_ids=[job_id], client=FakeClient({**AI, "score": 20, "ai_tier": "non_ai"}))
    assert get(db_url, job_id).status == JobStatus.shortlisted


def test_rescore_only_touches_the_ids_given(db_url):
    a, b = add_job(db_url), add_job(db_url, jd_text=JD + " B")
    shortlist.run(db_url=db_url, client=FakeClient(AI, AI))
    new_prompt(db_url)
    client = FakeClient({**AI, "score": 70, "ai_tier": "adjacent"})
    shortlist.run(db_url=db_url, job_ids=[a], client=client)
    assert len(client.prompts) == 1 and get(db_url, a).score == 70 and get(db_url, b).score == 78


# ---- role-family gate ------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Team member - Sterile Operations", "Warehouse Executive", "Clinical Research Nurse", "Plant Manager", "Lab Technician",
])
def test_role_gate_rejects_clearly_off_profile_titles(title):
    result = check_gates("Hyderabad", None, None, title=title)
    assert not result.passed and result.reason.startswith("off-profile role")


@pytest.mark.parametrize("title", [
    "Business Analyst", "Clinical Data Analyst", "Operations Analyst", "Product Owner - Manufacturing Platforms",
    "AI Product Manager", "Operations Manager", "Senior Executive",   # no off-profile word: Gemini decides
])
def test_role_gate_lets_business_and_unclear_titles_through(title):
    assert check_gates("Hyderabad", None, None, title=title).passed


def test_role_gate_uses_your_extra_words():
    cfg = GateConfig(avoid_title_words=["pharma representative"])
    assert not check_gates("Hyderabad", None, None, cfg, title="Pharma Representative").passed
    assert check_gates("Hyderabad", None, None, title="Pharma Representative").passed


def test_shortlist_rejects_an_off_profile_job_without_calling_gemini(db_url):
    off = add_job(db_url, title="Team member - Sterile Operations")
    ok = add_job(db_url, title="Operations Analyst")
    client = FakeClient(AI)
    shortlist.run(db_url=db_url, client=client)
    job = get(db_url, off)
    assert job.status == JobStatus.rejected and job.notes.startswith(GATE_PREFIX) and "off-profile" in job.notes
    assert len(client.prompts) == 1 and get(db_url, ok).status != JobStatus.rejected


# ---- 16 Oct: stronger role gate, title-ranked order, limit 60 ------------------------

@pytest.mark.parametrize("title", ["Management Trainee (BBA/MBA)", "Customer & Employee Experience Specialist (CSAT, ESAT, and NPS surveys)",
                                   "Digital Marketing Specialist", "Senior Accountant", "Sr. Manager - FP&A", "Inside Sales Executive"])
def test_role_gate_rejects_the_off_profile_titles_the_first_real_run_let_through(title):
    assert not check_gates("Hyderabad", 3, 5, title=title).passed


@pytest.mark.parametrize("title", ["AI Automation Intern", "Data Annotation Analyst", "Associate Product Analyst - Intern",
                                   "Business Analyst Trainee", "MBA Internship in Strategy", "Data Labelling Analyst"])
def test_role_gate_rejects_interns_trainees_and_annotation_even_with_a_profile_word(title):
    assert not check_gates("Hyderabad", 3, 5, title=title).passed


@pytest.mark.parametrize("title", ["Customer Insights Analyst", "Support Business Analyst", "Marketing Analytics Consultant",
                                   "Research Analyst", "Product Owner", "Finance Business Analyst", "Data Analyst", "Internal Audit Business Analyst"])
def test_role_gate_keeps_titles_with_a_profile_word(title):
    assert check_gates("Hyderabad", 3, 5, title=title).passed


def test_unscored_jobs_are_taken_in_title_relevance_order_not_newest_first(db_url):
    engineer = add_job(db_url, title="Java Developer")
    neutral = add_job(db_url, title="Digital Specialist")
    core_old = add_job(db_url, title="Business Analyst")
    core_new = add_job(db_url, title="Product Owner")
    with Session(get_engine(db_url)) as s:
        order = [j.id for j in shortlist.select_jobs(s, 10, None)]
        assert order[-1] == engineer and order[-2] == neutral
        assert set(order[:2]) == {core_old, core_new} and order[0] == core_new      # newest first inside a rank
        assert [j.id for j in shortlist.select_jobs(s, 1, None)] == [core_new]


def test_default_limit_is_60():
    import inspect
    assert inspect.signature(shortlist.run).parameters["limit"].default == 60
