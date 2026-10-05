"""
Gemini REST client for the free tier (stdlib only, like the legacy agent's).

- Token bucket before every request (GEMINI_RPM).
- 429 / 5xx: exponential backoff with jitter, honouring the server's
  RetryInfo delay when it sends one.
- Same generate_json(system, prompt) protocol as the legacy LLMClient, so
  the legacy tailoring code can use this client unchanged (Phase 6).
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from jobpilot.config import get_config
from jobpilot.llm.ratelimit import TokenBucket

logger = logging.getLogger(__name__)

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RETRY_STATUS = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    pass


class LLMConfigError(LLMError):
    """Missing key, unknown model, bad request: every call would fail the same way."""


class QuotaExhausted(LLMError):
    """Still rate-limited after every retry: stop this run, try again later."""


def parse_json_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise LLMError(f"Model did not return valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise LLMError("Model JSON must be an object")
    return data


def _retry_delay(detail: str) -> float | None:
    """Gemini puts e.g. "retryDelay": "17s" in its 429 body."""
    match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', detail)
    return float(match.group(1)) if match else None


class GeminiClient:
    def __init__(self, api_key: str | None = None, model: str | None = None, rpm: int | None = None,
                 temperature: float = 0.2, max_retries: int = 5, base_delay: float = 4.0, max_delay: float = 90.0,
                 timeout: float = 120.0, sleep: Callable[[float], None] = time.sleep,
                 opener: Callable[..., Any] = urllib.request.urlopen) -> None:
        config = get_config()
        self.api_key = api_key if api_key is not None else config.gemini_api_key
        self.model_name = model or config.gemini_model
        if not self.api_key:
            raise LLMConfigError("GEMINI_API_KEY is not set in E:\\JobPilot\\.env (free key: https://aistudio.google.com/apikey)")
        if not self.model_name:
            raise LLMConfigError("GEMINI_MODEL is not set in E:\\JobPilot\\.env")
        self.bucket = TokenBucket(rpm or config.gemini_rpm, sleep=sleep)
        self.temperature, self.max_retries = temperature, max_retries
        self.base_delay, self.max_delay, self.timeout = base_delay, max_delay, timeout
        self.sleep, self.opener = sleep, opener
        self.calls = 0

    def generate_json(self, system: str, prompt: str) -> dict[str, Any]:
        return parse_json_text(self.generate_text(system, prompt, json_mode=True))

    def generate_text(self, system: str, prompt: str, json_mode: bool = False) -> str:
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": self.temperature},
        }
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        request = urllib.request.Request(
            ENDPOINT.format(model=self.model_name), data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
        )

        last = ""
        for attempt in range(self.max_retries + 1):
            self.bucket.acquire()
            self.calls += 1
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    return self._extract_text(json.loads(response.read().decode("utf-8")))
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", "replace")[:2000]
                if error.code not in RETRY_STATUS:
                    kind = LLMConfigError if error.code in (400, 401, 403, 404) else LLMError
                    raise kind(f"Gemini HTTP {error.code}: {detail[:300]}") from error
                last = f"HTTP {error.code}"
                hinted = _retry_delay(detail) if error.code == 429 else None
            except (urllib.error.URLError, TimeoutError) as error:
                last, hinted = f"network: {error}", None
            if attempt == self.max_retries:
                break
            delay = min(self.max_delay, hinted or self.base_delay * 2 ** attempt) * random.uniform(1.0, 1.25)
            logger.info("Gemini %s; retry %s/%s in %.0fs", last, attempt + 1, self.max_retries, delay)
            self.sleep(delay)

        if last.startswith("HTTP 429"):
            raise QuotaExhausted(f"Gemini still rate-limited after {self.max_retries} retries")
        raise LLMError(f"Gemini failed after {self.max_retries} retries: {last}")

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError(f"Empty response ({(data.get('promptFeedback') or {}).get('blockReason', 'no candidates')})")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        if not text.strip():
            raise LLMError(f"Empty text (finishReason={candidates[0].get('finishReason')})")
        return text
