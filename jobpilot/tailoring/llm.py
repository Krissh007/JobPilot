"""
Minimal LLM clients. Only the standard library is used, so no SDK is needed.

Every client implements ``generate_json(system, prompt) -> dict``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Raised when the model cannot produce a usable answer."""


class LLMClient(Protocol):
    model_name: str

    def generate_json(self, system: str, prompt: str) -> dict[str, Any]: ...


def read_api_key(env_var: str, project_root: Path | None = None) -> str:
    """Read the key from the environment, else from a .env file in the project."""
    value = os.environ.get(env_var, "").strip()
    if value:
        return value
    if project_root is not None:
        env_file = project_root / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                key, _, raw = line.partition("=")
                if key.strip() == env_var:
                    return raw.strip().strip('"').strip("'")
    return ""


def parse_json_text(text: str) -> dict[str, Any]:
    """Parse model output that should be JSON, tolerating code fences."""
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("Model did not return JSON") from None
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as error:
            raise LLMError(f"Model returned invalid JSON: {error}") from error
    if not isinstance(data, dict):
        raise LLMError("Model JSON must be an object")
    return data


class GeminiClient:
    """Google Gemini REST API (works with a free Google AI Studio key)."""

    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(
        self,
        api_key: str,
        model: str,
        fallback_models: list[str] | None = None,
        temperature: float = 0.3,
        max_retries: int = 3,
        timeout_seconds: int = 120,
    ) -> None:
        if not api_key:
            raise LLMError("No Gemini API key configured")
        self.api_key = api_key
        self.models = [model, *(fallback_models or [])]
        self.model_name = model
        self.temperature = temperature
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds

    def generate_json(self, system: str, prompt: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for model in self.models:
            try:
                text = self._call_with_retries(model, system, prompt)
                self.model_name = model
                return parse_json_text(text)
            except LLMError as error:
                last_error = error
                logger.warning("Gemini model %s failed: %s", model, error)
        raise LLMError(f"All Gemini models failed; last error: {last_error}")

    def _call_with_retries(self, model: str, system: str, prompt: str) -> str:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "responseMimeType": "application/json",
            },
        }
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.ENDPOINT.format(model=model),
            data=payload,
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            method="POST",
        )

        delay = 10.0
        for attempt in range(1, self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    data = json.loads(response.read().decode("utf-8"))
                return self._extract_text(data)
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", "replace")[:300]
                if error.code in self.RETRY_STATUS and attempt < self.max_retries:
                    logger.info("Gemini %s returned %s; retrying in %.0fs", model, error.code, delay)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise LLMError(f"HTTP {error.code}: {detail}") from error
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt < self.max_retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise LLMError(f"Network error: {error}") from error
        raise LLMError("Retries exhausted")

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMError(f"Empty response ({reason})")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        if not text.strip():
            raise LLMError(f"Empty text (finishReason={candidates[0].get('finishReason')})")
        return text


def create_client(settings: Any, project_root: Path) -> LLMClient:
    """Build the configured client from ``settings.tailoring``."""
    provider = settings.provider.lower()
    if provider == "gemini":
        api_key = read_api_key(settings.api_key_env, project_root)
        if not api_key:
            raise LLMError(
                f"Set {settings.api_key_env} (environment variable or a line "
                f"'{settings.api_key_env}=...' in the project's .env file). "
                "Get a free key at https://aistudio.google.com/apikey"
            )
        return GeminiClient(
            api_key=api_key,
            model=settings.model,
            fallback_models=settings.fallback_models,
            temperature=settings.temperature,
        )
    raise LLMError(f"Unknown tailoring provider {settings.provider!r}")
