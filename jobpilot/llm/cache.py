"""LLM result cache: the same JD scored against the same resume never costs a second call."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlmodel import Session

from jobpilot.db.models import LLMCache


def cache_key(kind: str, jd_text: str, resume_version: str) -> str:
    # kind keeps scorer and (later) docs/answer results apart for the same JD.
    return hashlib.sha256(f"{kind}\x00{jd_text}\x00{resume_version}".encode("utf-8")).hexdigest()


def cache_get(session: Session, key: str) -> Any | None:
    row = session.get(LLMCache, key)
    return row.response if row is not None else None


def cache_put(session: Session, key: str, kind: str, response: Any) -> None:
    session.merge(LLMCache(key=key, kind=kind, response=response))
