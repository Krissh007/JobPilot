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
