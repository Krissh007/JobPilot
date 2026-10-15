"""
Truth checks and shared constants for tailored documents: every role's dates must be the master
resume's dates for that company, every title must exist in it, and Word's 255-character
property limit is respected.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from jobpilot.config import ROOT, application_profile_path

PROMPT_VERSION = "docs:v1"  # unchanged so cached model answers stay valid
OUTPUT_DIR = ROOT / "output"
MIN_JD_CHARS = 300
WORD_PROPERTY_MAX = 255  # Word rejects longer core properties (title, keywords)
DATE_RANGE = re.compile(
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}\s*[–—-]\s*"
    r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}|Present|Current)",
    re.IGNORECASE,
)


class DocsError(RuntimeError):
    """This job's documents could not be made truthfully; nothing was written."""


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def master_dates_for(company: str, master: str) -> list[str]:
    """Date ranges written next to this company in the master resume (its line + the 3 after)."""
    lines = master.splitlines()
    target = norm(company)
    words = [w for w in target.split() if w not in {"pvt", "ltd", "limited", "private", "inc", "llp"}]
    found: list[str] = []
    for i, line in enumerate(lines):
        normalized = norm(line)
        if target and (target in normalized or (words and all(w in normalized.split() for w in words))):
            for nearby in lines[i : i + 4]:
                found += [m.group(0) for m in DATE_RANGE.finditer(nearby)]
    return list(dict.fromkeys(found))


def verify_titles_and_dates(application: Any, master: str) -> list[str]:
    """Correct dates the model changed; refuse titles the master resume doesn't have."""
    notes: list[str] = []
    master_norm = norm(master)
    for entry in application.experience:
        if entry.title and norm(entry.title) not in master_norm:
            raise DocsError(f"Title '{entry.title}' at {entry.company} is not in the master resume")
        if entry.dates and norm(entry.dates) in master_norm:
            continue
        options = master_dates_for(entry.company, master)
        if len(options) == 1:
            notes.append(f"Corrected dates for {entry.company}: '{entry.dates or '(none)'}' -> '{options[0]}'")
            entry.dates = options[0]
        else:
            raise DocsError(f"Could not verify the dates '{entry.dates}' for {entry.company} against the master resume")
    return notes


def contact_details() -> dict[str, str]:
    """Name/email/phone/location from config/application_profile.yaml (git-ignored)."""
    path = application_profile_path()
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {k: str(v or "").strip() for k, v in (data.get("contact") or {}).items()}
