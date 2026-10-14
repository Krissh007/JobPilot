"""Years of experience stated in a job description, for jobs whose card showed none."""

from __future__ import annotations

import re

_RANGE = re.compile(r"(\d{1,2})\s*(?:-|to|–)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)
_MIN = re.compile(r"(?:minimum|min\.?|at least|over)?\s*(\d{1,2})\s*\+\s*(?:years?|yrs?)|(?:minimum|at least)\s*(?:of\s*)?(\d{1,2})\s*(?:years?|yrs?)", re.I)
_NEAR = 60  # characters around the match that must mention experience


def _near_experience(text: str, start: int, end: int) -> bool:
    return "experience" in text[max(0, start - _NEAR): end + _NEAR].lower()


def experience_from_jd(text: str) -> tuple[int | None, int | None]:
    """(min, max) from the first 'N-M years' / 'N+ years' phrase next to the word experience; (None, None) if none."""
    for m in _RANGE.finditer(text or ""):
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo <= hi <= 40 and _near_experience(text, m.start(), m.end()):
            return lo, hi
    for m in _MIN.finditer(text or ""):
        lo = int(m.group(1) or m.group(2))
        if lo <= 40 and _near_experience(text, m.start(), m.end()):
            return lo, None
    return None, None
