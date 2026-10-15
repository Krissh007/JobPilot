"""Shared fixtures for the document tests: a small master resume, a model answer, and a fake Gemini."""

from __future__ import annotations

import copy

MASTER = """KRISHNA TEST
AI Business Analyst
PROFESSIONAL EXPERIENCE
LOGNORMAL ANALYTICS PVT. LTD.
Business Analyst | AI Product Analyst
Hyderabad, India | Oct 2025 – Sep 2026
- Gathered requirements for LLM features with 12 stakeholders using SQL and Power BI
SOFTIC DIGITECH PVT. LTD.
Business Analyst
Hyderabad, India | Feb 2024 – Sep 2025
- Wrote BRDs and user stories in Jira for an analytics platform
EDUCATION
Bachelor of Engineering, Electronics & Communication Engineering
Gandhi Institute of Technology and Management, India
SKILLS
SQL, Power BI, Jira, Requirements Gathering, User Stories, Prompt Engineering
"""

GOOD = {
    "candidate": {"name": "Krishna Test", "contact_line": ""},
    "headline": "AI Business Analyst",
    "summary": "Business analyst who gathers requirements for LLM features.",
    "skills": ["SQL", "Power BI", "Requirements Gathering", "Kubernetes"],  # Kubernetes is NOT in the resume
    "experience": [
        {"title": "Business Analyst | AI Product Analyst", "company": "LogNormal Analytics Pvt. Ltd.", "location": "Hyderabad",
         "dates": "Oct 2025 – Sep 2026", "bullets": ["Gathered LLM requirements with 12 stakeholders using SQL", "Cut costs by 40%"]},
        {"title": "Business Analyst", "company": "Softic Digitech Pvt. Ltd.", "location": "Hyderabad",
         "dates": "Feb 2024 – Sep 2025", "bullets": ["Wrote BRDs and user stories in Jira"]},
    ],
    "projects": [],
    "education": [{"degree": "Bachelor of Engineering, Electronics & Communication Engineering",
                   "institution": "Gandhi Institute of Technology and Management", "dates": "", "details": ""}],
    "certifications": [],
    "cover_letter": {"salutation": "Dear Hiring Manager,", "paragraphs": ["I gather LLM requirements.", "I write BRDs in Jira."], "closing": "Sincerely,"},
    "jd_keywords": ["SQL", "LLM", "Tableau"],
    "missing_keywords": ["Tableau"],
}


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), 0

    def generate_json(self, system, prompt):
        self.calls += 1
        assert "MASTER RESUME" in prompt and "only source of facts" in prompt  # truth-first prompt is used
        return copy.deepcopy(self.responses.pop(0))
