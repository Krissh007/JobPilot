"""Gemini client, token bucket and error handling (no network)."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from jobpilot.llm.gemini import GeminiClient, LLMError, QuotaExhausted, parse_json_text
from jobpilot.llm.ratelimit import TokenBucket

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
