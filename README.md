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

**There is one database: `data\jobpilot.db`.** The search agent runs two stages of the legacy agent's code
from `E:\job_search_agent`, unchanged and with the legacy agent's own Python:
- **Browser discovery:** it skips URLs JobPilot already has.
- **Rule-based matching:** the legacy quality gates apply here.

The matched jobs stream straight into `jobpilot.db`. The legacy save, tailor, export and sync stages are not
called, so the legacy `jobs.db`, its Excel exports and `project_sync` files are never written. The old
`jobs.db` is kept as an archive and was imported once.

```powershell
.\.venv\Scripts\python -m jobpilot.agents.search                    # browser search, saved into jobpilot.db
.\.venv\Scripts\python -m jobpilot.agents.search --then-score       # search, then Gemini scoring
.\.venv\Scripts\python -m jobpilot.agents.search --keywords "AI Business Analyst" --sources naukri
.\.venv\Scripts\python -m jobpilot.agents.search --import-legacy-db # one-off: copy from the old jobs.db archive (read-only)
```

When jobs are copied from the old archive, statuses are mapped once: new/review → found, shortlisted,
applied, interviewing → interview, offer, skipped → rejected. Rows the legacy gates rejected are skipped.
Paths come from `LEGACY_AGENT_DIR` and `LEGACY_PYTHON` in `.env`.

Searching LinkedIn or Naukri with automation may break their user agreements. The legacy agent paces
its requests. Keep runs infrequent.

## Score agent

Gemini scores each unscored job description against your master resume. The resume defaults to the
legacy agent's `config\master_resume.md`; set `RESUME_PATH` in `.env` to change it. Each job gets an
overall score plus skills, experience and domain sub-scores, a reason, and lists of matched and missing skills.
Jobs at or above the threshold setting (default 60) move from found to shortlisted.

```powershell
.\.venv\Scripts\python -m jobpilot.agents.score              # up to 20 jobs, best legacy score first
.\.venv\Scripts\python -m jobpilot.agents.score --limit 5
.\.venv\Scripts\python -m jobpilot.agents.score --job-id 42  # re-score one job
.\.venv\Scripts\python -m jobpilot.agents.search --then-score
```

Free tier:
- A token bucket limits calls to `GEMINI_RPM`.
- HTTP 429/5xx are retried with exponential backoff, using Gemini's suggested delay when it sends one.
- Results are cached by `sha256(prompt version + JD + resume version)`. Editing the resume changes its version, so jobs are scored again.
- When the quota runs out, finished jobs are kept and the rest wait for the next run.
- A missing key or a wrong model name stops the run immediately as an error.
- Daily quotas are not retried. Some free-tier models allow only 20 requests a day (gemini-3.8-flash did).
  When the daily quota is used up, the run moves to the next model in `GEMINI_FALLBACK_MODELS`, or stops at once.
- Scoring uses `gemini-3.5-flash-lite` (set in `.env`). It is fast and its free quota is much larger, and it
  keeps the stronger model's small daily quota free for tailored documents.
- Domain is judged only from the role's own responsibilities. Naukri's "Department: Data Science &
  Analytics" label and the company's "About" text are ignored. Roles with no AI/ML component are capped at 70.

On Gemini's free tier, Google may use what you send (your resume and the JDs) to improve its products.

## Docs agent (tailored resume + cover letter)

```powershell
.\.venv\Scripts\python -m jobpilot.agents.docs              # up to 10 shortlisted jobs without documents
.\.venv\Scripts\python -m jobpilot.agents.docs --job-id 111 # (re)generate for one job
```

For each shortlisted job it writes `output\<Company>_<id>\` with these files:
- `<Name>_Resume_<Company>.docx`
- `<Name>_Cover_Letter_<Company>.docx`
- `match_report.md`: JD keywords covered and missing, plus every fact-check fix

It then moves the job to **Docs ready**.

**Truthfulness.** The legacy tailoring is reused unchanged. Its prompt allows only facts from the master resume, and its fact check removes:
- skills, companies, institutions and certifications that aren't in the resume;
- bullets and sentences containing numbers that aren't in the resume.

JobPilot adds one more check:
- Every role's dates must match your resume's dates for that company. Dates that don't match are corrected and the fix is logged.
- A job title that isn't in your resume fails that job, so nothing is written for it.

Anything the JD asks for that your resume doesn't show is listed in the report and never added.

Your resume is `RESUME_PATH` in `.env`; scoring uses the same file. When you save a new version, point `RESUME_PATH` at it. The new content gets a new version hash, so documents and scores are regenerated instead of coming from the cache. Contact details come from the legacy `config\application_profile.yaml`. Review every document before you upload it.

## API (backend for the dashboard)

```powershell
powershell -ExecutionPolicy Bypass -File E:\JobPilot\scripts\run_api.ps1
```

Interactive docs: http://127.0.0.1:8000/docs. The API listens on 127.0.0.1 only, and it also refuses
any request that doesn't come from this computer.

- **Pipeline:** `GET /api/meta` (rail counts, applied today vs cap), plus `/api/jobs` with filters (`status`, `source`,
  `min_score`, `q` full-text search, `include_examples`), sorting and paging. `PATCH /api/jobs/{id}`
  changes status or notes, and every status change is written to history with actor `user`.
- **Agents:** `POST /api/runs/{search|score}` starts the agent as a background process and returns its run id.
  `GET /api/runs/{id}/stream` is a Server-Sent Events stream of its log. It tails the database, so runs started by
  the scheduler can be watched too. A second start of the same agent is refused while one is running.
  Each process's console output goes to `data\logs\run_<id>.log`.
- **Review queue:** `POST /api/applications/{id}/approve` or `/skip` only works while the application is waiting
  for review. The apply agent (Phase 7) is the only thing that touches the browser.
- **Other routes:** answers CRUD, settings (validated: threshold 30–95, daily cap 1–40), analytics (funnel, score
  histogram, applications per day), and CSV/JSON export.

## Dashboard

```powershell
cd E:\JobPilot\web
$env:npm_config_cache = "E:\JobPilot\.cache\npm"
npm install                                   # first time only
powershell -ExecutionPolicy Bypass -File E:\JobPilot\scripts\run_api.ps1   # terminal 1
powershell -ExecutionPolicy Bypass -File E:\JobPilot\scripts\run_web.ps1   # terminal 2 → http://127.0.0.1:5173
```

The dashboard is built with React, Vite, Tailwind and TypeScript. It uses the "Ledger" layout with the "Flight deck" palette and type, in light and dark mode (the header toggle cycles system → light → dark).

| Screen | What it does |
|---|---|
| **Pipeline rail** | Stage counts (click one to filter), a proportional bar, the threshold slider (saved when you release it), and applied today vs the cap |
| **Board** | Dense rows per stage. Drag a row to another column to change its status, and the move is written to history |
| **Jobs** | Sortable table: score, role, company, location, experience, source, rule score, status, found |
| **Job drawer** | Score and sub-score bars, the reason, matched skills, status, notes (saved when you leave the field), posting link, documents, JD, history, delete |
| **Review** | Filled fields with where each value came from (Gemini drafts and empty or consent fields highlighted), a screenshot, and Approve submit / Skip. Approve is locked while a form is stuck before LinkedIn's Submit page. Blocked applications are listed separately |
| **Runs** | Run now for search, score, docs and apply (apply has a review-only option), with a live log over Server-Sent Events, plus run history with logs |
| **Answers** | Add, edit and delete screening answers; open questions the apply agent met are highlighted; the weekly review card |
| **Database** | Table cards with row counts, CSV/JSON export, import from the legacy archive, and removing or adding the example rows |
| **Settings** | Threshold, daily cap, search queries, LinkedIn Easy-Apply-only, the auto-submit switch, review wait, weekly review time, the Windows schedule (status, Register / Update / Remove), Gemini model and key status (the key is never shown), and the site-terms warning |

Keyboard: `/` jumps to search, `N` adds a job, `Esc` closes the drawer.

## Apply agent (LinkedIn Easy Apply)

```powershell
.venv\Scripts\python.exe -m jobpilot.agents.apply --review-only --limit 1   # fill one form, wait for you
.venv\Scripts\python.exe -m jobpilot.agents.apply --job-id 190 --review-only
.venv\Scripts\python.exe -m jobpilot.legacy.answers_import                 # one-off: answer bank from the legacy profile
```

- Uses your **installed Chrome** with the legacy signed-in profile (`E:\job_search_agent\data\browser_profile`), headed, one tab, a 20–90 s random pause between jobs, and the daily cap. It never types a password.
- Every field records its source: resume/profile, answer bank, or a Gemini draft (Gemini only answers from your resume and may leave a field empty). Unknown questions are added to the answer bank as open questions.
- **Consent, terms and privacy questions are never ticked or answered by the agent**, even if the answer bank has one.
- It stops on LinkedIn's Submit page, saves a screenshot, sends a Windows toast and waits (default 20 min) for Approve or Skip in the Review queue. If it couldn't reach the Submit page, the application is marked *incomplete*: finish it in the agent's Chrome window and either Approve (it unlocks once the form reaches Submit) or submit it there yourself; JobPilot records it either way.
- **Auto-submit** (Settings, off by default) submits without asking only when every field came from your resume/profile or answer bank, nothing is empty or consent-gated, and the job is Docs-ready with score ≥ threshold.
- CAPTCHA, checkpoint or login wall: the application is marked *blocked*, you get a toast, the window stays open and the run stops. Nothing tries to solve it.
- Jobs that are "Apply on company website" are remembered and never opened again; the drawer marks them *External apply*.
- **Site terms:** automating a signed-in LinkedIn or Naukri account is against both sites' user agreements and can get the account restricted. The low cap and pauses reduce that risk; they don't remove it.

## Scheduling (Windows Task Scheduler)

Register from **Settings → Schedule → Register with Windows**, or from a terminal:

```powershell
powershell -ExecutionPolicy Bypass -File E:\JobPilot\scripts\register_tasks.ps1            # register / update
powershell -ExecutionPolicy Bypass -File E:\JobPilot\scripts\register_tasks.ps1 -WhatIf    # preview
powershell -ExecutionPolicy Bypass -File E:\JobPilot\scripts\register_tasks.ps1 -Unregister
```

| Task | When (from Settings) | Runs |
|---|---|---|
| `JobPilot Search-Score-Docs` | daily 08:00, every 4 h until 22:00 IST | `pythonw -m jobpilot.scheduled --log`: search → score → docs, each recorded in Agent runs; log in `data\logs\scheduled.log` |
| `JobPilot Weekly-Answer-Review` | Monday 09:00 IST | `pythonw -m jobpilot.weekly`: toast if the answer review is due |

The tasks run only while you are logged on (search uses your signed-in browser profile), start a missed run when the PC is next available, never overlap (a lock file plus *ignore new instance*), stop after 2 h, and need no admin rights. **Apply is never scheduled**: it opens a browser and waits for you. After changing the times in Settings, click **Update tasks**.

