"""
Dedupe keys for jobs.

1. url_canonical: lower-cased host, no tracking params, no fragment or
   trailing slash. LinkedIn and Naukri URLs collapse to their job id.
2. dedupe_hash: fallback for the same job reached through different URLs,
   from normalised company + title + location.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAM = re.compile(
    r"^(utm_.*|ref|refid|trk|trkinfo|trackingid|src|srctype|sid|xp|px|fbclid|gclid|msclkid|"
    r"from|position|pagenum|lgid|ebp|recommendedflavor|alternatechannel|eid|origin|geoid)$",
    re.IGNORECASE,
)
LINKEDIN_JOB_ID = re.compile(r"/jobs/view/(?:[^/]*?-)?(\d{6,})")
NAUKRI_JOB_ID = re.compile(r"-(\d{9,})$")
COMPANY_SUFFIX = re.compile(
    r"\b(private|pvt|limited|ltd|llp|inc|incorporated|corp|corporation|co|india)\b\.?",
)


def portal_of(url: str) -> str | None:
    """'linkedin' / 'naukri' from the link's host (more reliable than a feed's source column), else None."""
    host = urlsplit(str(url or "").strip()).netloc.lower()
    for portal in ("linkedin", "naukri"):
        if host == f"{portal}.com" or host.endswith(f".{portal}.com"):
            return portal
    return None


def canonical_url(url: str) -> str:
    raw = str(url or "").strip()
    parts = urlsplit(raw)
    if not parts.netloc:
        return raw.lower().rstrip("/")

    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAM.match(k)]

    if host.endswith("linkedin.com"):
        current = dict(params).get("currentJobId")
        match = LINKEDIN_JOB_ID.search(path)
        job_id = match.group(1) if match else current
        if job_id:
            return f"https://linkedin.com/jobs/view/{job_id}"
    if host.endswith("naukri.com"):
        match = NAUKRI_JOB_ID.search(path)
        if match:
            return f"https://naukri.com/job-listings-{match.group(1)}"
        params = []  # Naukri query strings are tracking only

    query = urlencode(sorted(params))
    return urlunsplit(("https", host, path, query, ""))


def _norm(text: str) -> str:
    text = str(text or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(text.split())


def normalize_company(company: str) -> str:
    return " ".join(COMPANY_SUFFIX.sub(" ", _norm(company)).split())


def dedupe_hash(company: str, title: str, location: str) -> str:
    # Only the first location token: "Hyderabad, Telangana, India" == "Hyderabad".
    city = _norm(str(location or "").split(",")[0])
    key = "|".join((normalize_company(company), _norm(title), city))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
