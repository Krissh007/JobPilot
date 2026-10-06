from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest
from sqlmodel import Session, select

from jobpilot.agents import score
from jobpilot.db.models import Actor, AgentRun, Job, JobStatus, LLMCache, RunStatus, StatusHistory
from jobpilot.db.repo import put_setting, upsert_job
from jobpilot.db.session import get_engine
from jobpilot.llm.gemini import GeminiClient, LLMError, QuotaExhausted, parse_json_text
from jobpilot.llm.ratelimit import TokenBucket
from jobpilot.resume import Resume

GOOD = {"score": 78, "skills": 80, "experience": 75, "domain": 76, "reason": "Strong SQL and LLM requirements work.",
        "matched_skills": ["SQL", "LLM evaluation"], "missing_skills": ["Tableau"]}
JD = "We need an AI Business Analyst to own requirements for LLM features, write BRDs and analyse data in SQL. " * 3


class FakeClient:
    """Plays back queued responses: dicts are returned, exceptions are raised."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def generate_json(self, system, prompt):
        self.prompts.append(prompt)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def resume(tmp_path, monkeypatch) -> Resume:
    path = tmp_path / "master_resume.md"
    path.write_text("KRISHNA M\nAI Business Analyst, 3 years. SQL, Power BI, LLM evaluation, BRDs, Agile. " * 5, encoding="utf-8")
    value = Resume(path=path, text=path.read_text(encoding="utf-8"))
    monkeypatch.setattr(score, "load_resume", lambda: value)
    return value


def add_job(db_url, **overrides) -> int:
    data = {"title": "AI Business Analyst", "company": "Acme", "source": "naukri", "location": "Hyderabad",
            "url": "https://www.naukri.com/job-listings-x-150926500001", "jd_text": JD}
    data.update(overrides)
    with Session(get_engine(db_url)) as s:
        job, _ = upsert_job(s, data)
        s.commit()
        return job.id


def get(db_url, model, ident):
    with Session(get_engine(db_url)) as s:
        return s.get(model, ident)


def last_run(db_url) -> AgentRun:
    with Session(get_engine(db_url)) as s:
        return s.exec(select(AgentRun).order_by(AgentRun.id.desc())).first()


def test_scores_and_shortlists_above_threshold(db_url, resume):
    job_id = add_job(db_url)
    score.run(db_url=db_url, client=FakeClient(GOOD))

    job = get(db_url, Job, job_id)
    assert (job.score, job.score_skills, job.score_experience, job.score_domain) == (78, 80, 75, 76)
    assert job.skills == ["SQL", "LLM evaluation"] and job.resume_version == resume.version
    assert job.status == JobStatus.shortlisted
    with Session(get_engine(db_url)) as s:
        last = s.exec(select(StatusHistory).where(StatusHistory.job_id == job_id).order_by(StatusHistory.id.desc())).first()
    assert (last.from_status, last.to_status, last.actor) == (JobStatus.found, JobStatus.shortlisted, Actor.agent)
    run = last_run(db_url)
    assert run.status == RunStatus.ok and (run.found, run.processed) == (1, 1)


def test_below_threshold_stays_found(db_url, resume):
    job_id = add_job(db_url)
    with Session(get_engine(db_url)) as s:
        put_setting(s, "threshold", 80)
        s.commit()
    score.run(db_url=db_url, client=FakeClient(GOOD))
    assert get(db_url, Job, job_id).status == JobStatus.found
    assert (last_run(db_url).found, last_run(db_url).processed) == (0, 1)


def test_retries_once_on_bad_json(db_url, resume):
    job_id = add_job(db_url)
    client = FakeClient(LLMError("Model did not return valid JSON"), GOOD)
    score.run(db_url=db_url, client=client)
    assert get(db_url, Job, job_id).score == 78
    assert len(client.prompts) == 2 and "previous reply was rejected" in client.prompts[1]


def test_invalid_twice_leaves_job_unscored(db_url, resume):
    job_id = add_job(db_url)
    score.run(db_url=db_url, client=FakeClient({**GOOD, "score": 150}, {"score": "high"}))
    assert get(db_url, Job, job_id).score is None
    run = last_run(db_url)
    assert run.status == RunStatus.ok and run.processed == 0 and "FAILED after retry" in run.log


def test_cache_hit_skips_gemini(db_url, resume):
    first = add_job(db_url)
    score.run(db_url=db_url, client=FakeClient(GOOD))
    # Same JD reposted under another URL/company on the same portal: no second call.
    second = add_job(db_url, url="https://www.naukri.com/job-listings-y-150926500002", company="Beta")
    score.run(db_url=db_url, client=FakeClient())  # an empty client fails if called
    assert get(db_url, Job, second).score == 78 and "[cache]" in last_run(db_url).log
    with Session(get_engine(db_url)) as s:
        assert len(s.exec(select(LLMCache)).all()) == 1
    assert get(db_url, Job, first).score == 78


def test_resume_change_invalidates_cache(db_url, resume, monkeypatch):
    job_id = add_job(db_url)
    score.run(db_url=db_url, client=FakeClient(GOOD))
    edited = Resume(path=resume.path, text=resume.text + "\nNew: Tableau certification.")
    monkeypatch.setattr(score, "load_resume", lambda: edited)
    client = FakeClient({**GOOD, "score": 85})
    score.run(job_id=job_id, db_url=db_url, client=client)
    job = get(db_url, Job, job_id)
    assert len(client.prompts) == 1 and job.score == 85 and job.resume_version == edited.version


def test_quota_exhausted_keeps_finished_work_and_marks_error(db_url, resume):
    a = add_job(db_url, legacy_score=90)
    b = add_job(db_url, url="https://www.naukri.com/job-listings-z-150926500003", company="Gamma",
                jd_text=JD + " Different role details.", legacy_score=50)
    with pytest.raises(QuotaExhausted):
        score.run(db_url=db_url, client=FakeClient(GOOD, QuotaExhausted("still 429")))
    assert get(db_url, Job, a).score == 78 and get(db_url, Job, b).score is None
    run = last_run(db_url)
    assert run.status == RunStatus.error and "1 job(s) left for the next run" in run.log


def test_skips_examples_and_short_jds(db_url, resume):
    add_job(db_url, jd_text="Too short")
    add_job(db_url, url="https://example.com/ex", company="Example Co", is_example=True)
    score.run(db_url=db_url, client=FakeClient())
    assert "0 job(s) to score; 1 unscored without a usable JD skipped" in last_run(db_url).log


# ---- rate limiter and HTTP client (no network) ----

class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_token_bucket_spaces_requests():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_minute=6, capacity=1, clock=clock, sleep=clock.sleep)
    assert bucket.acquire() == 0
    assert bucket.acquire() == pytest.approx(10.0)  # 6 rpm = one every 10 s
    clock.now += 30
    assert bucket.acquire() == 0  # refilled (capped at capacity 1)


def http_error(code, body=""):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body.encode()))


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def ok_payload(text):
    return {"candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True}, {"text": text}]}}]}


def make_client(outcomes, clock):
    calls = iter(outcomes)

    def opener(request, timeout):
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)

    return GeminiClient(api_key="test", model="gemini-test", rpm=600, max_retries=3, sleep=clock.sleep, opener=opener)


def test_backoff_honours_retry_delay_then_succeeds():
    clock = FakeClock()
    body = '{"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]}}'
    client = make_client([http_error(429, body), http_error(503), ok_payload('```json\n{"a": 1}\n```')], clock)
    assert client.generate_json("sys", "prompt") == {"a": 1}
    waits = [s for s in clock.slept if s >= 1]
    assert 7 <= waits[0] <= 7 * 1.25  # server's hint
    assert 8 <= waits[1] <= 8 * 1.25  # base 4s * 2**1


def test_persistent_429_raises_quota_exhausted():
    client = make_client([http_error(429)] * 4, FakeClock())
    with pytest.raises(QuotaExhausted):
        client.generate_json("sys", "prompt")


def test_client_error_is_not_retried():
    client = make_client([http_error(400, "bad model")], FakeClock())
    with pytest.raises(LLMError, match="HTTP 400"):
        client.generate_json("sys", "prompt")
    assert client.calls == 1


def test_missing_key_is_a_clear_error(monkeypatch):
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiClient(api_key="", model="m")


def test_parse_json_rejects_non_objects():
    with pytest.raises(LLMError):
        parse_json_text("[1, 2]")


def test_config_error_stops_the_run_as_error(db_url, resume, monkeypatch):
    add_job(db_url)
    monkeypatch.setattr(score, "GeminiClient", lambda **kw: GeminiClient(api_key="", model="m"))
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        score.run(db_url=db_url)
    run = last_run(db_url)
    assert run.status == RunStatus.error and "GEMINI_API_KEY" in run.error


def test_bad_model_name_is_a_config_error(db_url, resume):
    from jobpilot.llm.gemini import LLMConfigError

    job_id = add_job(db_url)
    with pytest.raises(LLMConfigError):
        score.run(db_url=db_url, client=FakeClient(LLMConfigError("Gemini HTTP 404: model not found")))
    assert get(db_url, Job, job_id).score is None and last_run(db_url).status == RunStatus.error


DAILY_429 = json.dumps({"error": {"status": "RESOURCE_EXHAUSTED", "details": [
    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
     "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]},
    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "83883s"}]}})


def test_daily_quota_is_not_retried():
    clock = FakeClock()
    client = make_client([http_error(429, DAILY_429)], clock)
    with pytest.raises(QuotaExhausted, match=r"daily free-tier quota used up \(GenerateRequestsPerDayPerProjectPerModel-FreeTier=20\); resets in ~23h"):
        client.generate_json("sys", "prompt")
    assert client.calls == 1 and not [s for s in clock.slept if s >= 1]


def test_daily_quota_switches_to_fallback_model(monkeypatch):
    from jobpilot.config import Config

    monkeypatch.setattr("jobpilot.llm.gemini.get_config", lambda: Config(
        gemini_api_key="k", gemini_model="big-model", gemini_fallback_models="lite-model", gemini_rpm=600))
    urls = []
    outcomes = iter([http_error(429, DAILY_429), ok_payload('{"a": 2}'), ok_payload('{"a": 3}')])

    def opener(request, timeout):
        urls.append(request.full_url)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)

    notes = []
    client = GeminiClient(sleep=FakeClock().sleep, opener=opener, on_retry=notes.append)
    assert client.generate_json("s", "p") == {"a": 2} and client.model_name == "lite-model"
    assert client.generate_json("s", "p") == {"a": 3}  # stays on the fallback
    assert ["big-model" in urls[0], "lite-model" in urls[1], "lite-model" in urls[2]] == [True] * 3
    assert "switching to lite-model" in notes[0]
