"""
Agent 2 hard gates: a cheap pre-filter before Gemini, nothing more. There is no
rule-score floor; everything that passes goes to Gemini, the only judge.

    India only          location must name India, an Indian city/state, or be a bare "Remote";
                        a location that names only somewhere else fails; an empty one passes.
    Experience 3-7 yrs  the job's stated range must overlap [exp_min, exp_max];
                        unknown or open-ended ("5+") ranges pass when they can still fit.
    Role family         the title names an off-profile field (sterile operations, manufacturing, clinical, ...)
                        and nothing business/product/analyst-like. A title with neither kind of word passes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel

INDIA_MARKERS = (
    "india", "hyderabad", "secunderabad", "bengaluru", "bangalore", "chennai", "mumbai", "navi mumbai", "thane",
    "pune", "delhi", "new delhi", "gurgaon", "gurugram", "noida", "ghaziabad", "faridabad", "kolkata", "ahmedabad",
    "gandhinagar", "jaipur", "lucknow", "chandigarh", "mohali", "kochi", "cochin", "thiruvananthapuram", "trivandrum",
    "coimbatore", "indore", "bhopal", "nagpur", "visakhapatnam", "vizag", "vijayawada", "mysuru", "mysore",
    "bhubaneswar", "nashik", "vadodara", "surat", "patna", "ranchi", "dehradun", "mangaluru", "madurai",
    "telangana", "karnataka", "maharashtra", "tamil nadu", "andhra pradesh", "kerala", "gujarat", "haryana",
    "uttar pradesh", "rajasthan", "west bengal", "odisha", "punjab",
)
_INDIA = re.compile(r"\b(?:" + "|".join(re.escape(m) for m in INDIA_MARKERS) + r")\b", re.I)
_REMOTE_ONLY = re.compile(r"^\W*(?:remote|work from home|wfh|hybrid|anywhere)\W*$", re.I)


# Words that mark a role as outside business/product/analyst work. Kept short and specific: a title is only rejected
# when it has one of these AND none of the profile words, so "Operations Analyst" or "Clinical Data Analyst" still pass.
OFF_PROFILE_WORDS = (
    "sterile", "manufacturing", "production operator", "plant", "warehouse", "laboratory", "lab technician", "clinical",
    "nurse", "nursing", "pharmacist", "technician", "machine operator", "driver", "electrician", "mechanic", "welder",
    "housekeeping", "chef", "security guard", "telecaller", "field sales", "store manager", "teacher", "tutor",
)
PROFILE_WORDS = (
    "analyst", "analytics", "product", "business", "bsa", "ba", "data", "consultant", "strategy", "program",
    "project", "insight", "requirement", "owner", "scrum", "functional", "systems", "process", "ai",
)
_OFF = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in OFF_PROFILE_WORDS) + r")\b", re.I)
_PROFILE = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in PROFILE_WORDS) + r")\b", re.I)

GATE_PREFIX = "Gate:"  # fixed note prefix on jobs rejected by a gate


class GateConfig(BaseModel):
    exp_min: int = 3
    exp_max: int = 7
    avoid_title_words: list[str] = []   # extra off-profile words you add in Settings (scoring.avoid_title_words)


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str = ""


def india_gate(location: str) -> GateResult:
    text = (location or "").strip()
    if not text or _REMOTE_ONLY.match(text) or _INDIA.search(text):
        return GateResult(True)
    return GateResult(False, f"not India ({text[:60]})")


def experience_gate(exp_min: int | None, exp_max: int | None, config: GateConfig) -> GateResult:
    if exp_min is None and exp_max is None:
        return GateResult(True)
    low = exp_min if exp_min is not None else exp_max
    high = exp_max  # None = open-ended ("5+ years")
    if high is not None and high < config.exp_min:
        return GateResult(False, f"experience {low}-{high} yrs is below {config.exp_min}-{config.exp_max}")
    if low is not None and low > config.exp_max:
        span = f"{low}+" if high is None else f"{low}-{high}"
        return GateResult(False, f"experience {span} yrs is above {config.exp_min}-{config.exp_max}")
    return GateResult(True)


def role_gate(title: str, config: GateConfig | None = None) -> GateResult:
    text = (title or "").strip()
    extra = [w.strip() for w in (config.avoid_title_words if config else []) if w.strip()]
    off = _OFF.search(text) or (re.search(r"\b(?:" + "|".join(re.escape(w) for w in extra) + r")\b", text, re.I) if extra else None)
    if off and not _PROFILE.search(text):
        return GateResult(False, f"off-profile role ({off.group(0).lower()}): {text[:60]}")
    return GateResult(True)


def check_gates(location: str, exp_min: int | None, exp_max: int | None, config: GateConfig | None = None,
                title: str = "") -> GateResult:
    config = config or GateConfig()
    for result in (india_gate(location), experience_gate(exp_min, exp_max, config), role_gate(title, config)):
        if not result.passed:
            return result
    return GateResult(True)
