from __future__ import annotations

from typing import Any

SYSTEM_PROMPT = """\
You are an expert resume writer and ATS (applicant tracking system) specialist.
You tailor a candidate's EXISTING resume and write a cover letter for one job.

ABSOLUTE RULES - truthfulness:
1. Use ONLY facts found in the MASTER RESUME. Never invent employers, job titles,
   dates, degrees, certifications, tools, skills, metrics, numbers or achievements.
2. Keep every employer name, job title, date range, degree and institution EXACTLY
   as written in the master resume.
3. You MAY: reorder sections, bullets and skills; rephrase bullets to use the job
   description's wording WHEN the master resume shows the same work; merge or
   shorten bullets; drop content that is irrelevant to this job; write a new
   summary built only from master-resume facts.
4. If the job asks for something the master resume does not show, do NOT add it.
   List it in "missing_keywords" instead.
5. Keep metrics exactly as in the master resume. Never create new numbers.

ATS RULES:
- Use exact keyword phrases from the job description where they are truthful
  (e.g. if the JD says "requirements elicitation" and the resume shows
  requirements gathering, you may use "requirements elicitation/gathering").
- Plain text only: no emojis, tables, icons, columns or special symbols.
- Bullets start with a strong action verb, one line to two lines each,
  3-6 bullets per recent role, fewer for older roles.
- Summary: 3-4 sentences, mention the target job title naturally.
- Skills: 10-18 items, most job-relevant first, only skills evidenced in the
  master resume.

COVER LETTER RULES:
- 250-350 words, 3-4 body paragraphs, professional and specific to the company
  and role. Reference 2-3 concrete master-resume achievements that map to the
  job's key requirements. No invented facts, no clichés like "I am writing to
  express my interest". Do not mention salary or notice period.

Return ONLY a JSON object, no commentary."""


OUTPUT_SCHEMA = """\
{
  "candidate": {
    "name": "exactly as in master resume",
    "contact_line": "email | phone | city | LinkedIn - only items present in master resume"
  },
  "headline": "short professional title aligned to the target role, truthful",
  "summary": "3-4 sentence professional summary",
  "skills": ["skill", "..."],
  "experience": [
    {
      "title": "exact title", "company": "exact company", "location": "as in resume or empty",
      "dates": "exact dates", "bullets": ["...", "..."]
    }
  ],
  "projects": [{"name": "exact project name", "bullets": ["..."]}],
  "education": [{"degree": "exact", "institution": "exact", "dates": "exact or empty", "details": ""}],
  "certifications": ["exact certification names only"],
  "cover_letter": {
    "salutation": "Dear Hiring Manager,",
    "paragraphs": ["...", "...", "..."],
    "closing": "Sincerely,"
  },
  "jd_keywords": ["the 15-25 most important skills/keywords/phrases in the job description"],
  "missing_keywords": ["JD requirements the master resume does NOT support"]
}"""


def build_prompt(job: dict[str, Any], master_resume: str) -> str:
    description = str(job.get("description") or job.get("card_description") or "").strip()
    return f"""\
TARGET JOB
Title: {job.get('title') or ''}
Company: {job.get('company') or ''}
Location: {job.get('location') or ''}
Experience required: {job.get('experience') or ''}

JOB DESCRIPTION
{description[:12000]}

MASTER RESUME (the only source of facts)
{master_resume[:15000]}

Produce the tailored resume and cover letter for this job as JSON with exactly
this structure (use empty lists or strings when the master resume has nothing
for a section):
{OUTPUT_SCHEMA}"""
