# JobPilot

A local dashboard and SQLite pipeline on top of the existing job search agent (`E:\job_search_agent`).
JobPilot never submits an application without your approval in the dashboard.

> This is a work in progress. See `PROGRESS.md`. The full Windows setup guide comes in Phase 9.

## Setup so far (Windows, PowerShell)

```powershell
cd E:\JobPilot
python -m venv .venv
$env:PIP_CACHE_DIR = "E:\JobPilot\.cache\pip"   # keep caches off C:
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env                            # then add GEMINI_API_KEY and GEMINI_MODEL
.\.venv\Scripts\alembic upgrade head
.\.venv\Scripts\python -m jobpilot.db.seed        # optional example data
.\.venv\Scripts\python -m jobpilot.db.seed --purge
.\.venv\Scripts\python -m pytest
```

## Search agent

The search agent runs the legacy agent's own discovery pipeline from `E:\job_search_agent` (using the legacy
agent's Python). For that run only, its tailoring and form preparation are turned off. JobPilot then
imports the legacy `data\jobs.db` **read-only**. If that database is unavailable, it imports the newest
`new_leads_*.xlsx` export instead.

```powershell
.\.venv\Scripts\python -m jobpilot.agents.search                    # full search + import (opens a browser)
.\.venv\Scripts\python -m jobpilot.agents.search --import-only      # import only
.\.venv\Scripts\python -m jobpilot.agents.search --keywords "AI Business Analyst" --sources naukri
```

Legacy statuses are mapped only when a job is first imported: new/review → found, shortlisted,
applied, interviewing → interview, offer, skipped → rejected. Rows the legacy gates rejected are not imported.
After that first import, JobPilot owns the stage. Paths come from `LEGACY_AGENT_DIR` and `LEGACY_PYTHON` in `.env`.

Searching LinkedIn or Naukri with automation may break their user agreements. The legacy agent paces
its requests. Keep runs infrequent.
