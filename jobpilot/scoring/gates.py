"""
Agent 2 hard gates: a cheap pre-filter before Gemini, nothing more. There is no
rule-score floor; everything that passes goes to Gemini, the only judge.

    India only          location must name India, an Indian city/state, or be a bare "Remote";
                        a location that names only somewhere else fails; an empty one passes.
    Experience 3-7 yrs  the job's stated range must overlap [exp_min, exp_max];
                        unknown or open-ended ("5+") ranges pass when they can still fit.
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


class GateConfig(BaseModel):
    exp_min: int = 3
    exp_max: int = 7


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


def check_gates(location: str, exp_min: int | None, exp_max: int | None, config: GateConfig | None = None) -> GateResult:
    config = config or GateConfig()
    for result in (india_gate(location), experience_gate(exp_min, exp_max, config)):
        if not result.passed:
            return result
    return GateResult(True)
