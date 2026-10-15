# JobPilot

A local dashboard and SQLite pipeline with its own search, scoring, documents and apply code:
search LinkedIn and Naukri, score every job against your resume with Gemini, write a fact-checked
tailored resume and cover letter, and apply (LinkedIn Easy Apply and Naukri) one job at a time.
**Nothing is submitted without your approval** unless you switch on auto-submit, and even then only
when every answer came from your resume or answer bank.

How it is built: [`docs/DESIGN.md`](docs/DESIGN.md). Build history: [`PROGRESS.md`](PROGRESS.md).

## Daily use

```powershell
powershell -ExecutionPolicy Bypass -File E:\JobPilot\scripts\run_api.ps1     # then open http://127.0.0.1:8000
```

- One server serves the API and the dashboard. If it is already running the script says so; add
  `-Restart` after an update to replace the running copy. It only prints warnings and errors; add
  `-Verbose` to see every request (the open dashboard polls every few seconds).
- Search, scoring and documents run by themselves (see *Scheduling*). You look at **Review** when a toast
  says an application is waiting, answer new **open questions** on the Answers tab (a reminder comes every
  Monday 09:00), and follow progress on **Board**, **Jobs** and **Analytics**.

## First-time setup (Windows, PowerShell)

C: is nearly full on this PC, so every cache lives on E:.

```powershell
cd E:\JobPilot
python -m venv .venv
$env:PIP_CACHE_DIR = "E:\JobPilot\.cache\pip"
.\.venv\Scripts\pip install -r requirements.txt     # includes Playwright; it drives your installed Chrome (no browser download)
copy .env.example .env                              # then type GEMINI_API_KEY yourself; GEMINI_MODEL, RESUME_PATH as needed
.\.venv\Scripts\alembic upgrade head                # creates data\jobpilot.db (WAL, FTS5)

cd web
$env:npm_config_cache = "E:\JobPilot\.cache\npm"
npm install
npm run build                                       # the API serves web\dist at http://127.0.0.1:8000
cd ..

.\.venv\Scripts\python -m jobpilot.agents.apply.answers_import   # answer bank from config\application_profile.yaml

# Sign in once per portal in the agent's own browser profile (you type the password or OTP; JobPilot never sees it):
cd E:\JobPilot
python -m jobpilot.agents.apply.login linkedin
python -m jobpilot.agents.apply.login naukri

powershell -ExecutionPolicy Bypass -File E:\JobPilot\scripts\register_tasks.ps1   # or Settings -> Schedule -> Register
```

Optional: `.\.venv\Scripts\python -m jobpilot.db.seed` adds 15 fictional example jobs (marked *example*);
`--purge` or Database -> Remove examples deletes them again.

## Discovery agent

**There is one database: `data\jobpilot.db`.** Discovery searches Naukri (paging by clicking Next) and LinkedIn,
ranks the result cards by title (core BA / product-analyst titles first, engineering titles last), skips
what the database already has and saves new jobs as `found`. Then it fetches descriptions for the leftover
unenriched jobs, freshest first, and expires the ones past the freshness window.

```powershell
.\.venv\Scripts\python -m jobpilot.agents.discover                              # filters from Settings
.\.venv\Scripts\python -m jobpilot.agents.discover --sources naukri --keywords "Business Analyst" --max-pages-per-query 3
.\.venv\Scripts\python -m jobpilot.agents.discover --dry-run                    # print the planned queries, open no browser
```

**Run now** (dashboard header, or `python -m jobpilot.scheduled --now`) runs all four agents in order: Discovery,
Scoring, Documents, then Apply in review-only mode. Each agent logs under its own name in the Runs screen.

Searching LinkedIn or Naukri with automation may break their user agreements. Keep runs infrequent.

## Scoring agent

Gemini scores each unscored job description against your master resume. The resume defaults to the
`config\master_resume.md`; set `RESUME_PATH` in `.env` to change it. Each job gets an
overall score plus skills, experience and domain sub-scores, a reason, and lists of matched and missing skills.
Jobs at or above the threshold setting (default 60) move from found to shortlisted.

```powershell
.\.venv\Scripts\python -m jobpilot.agents.shortlist              # up to 20 jobs
.\.venv\Scripts\python -m jobpilot.agents.shortlist --limit 5
.\.venv\Scripts\python -m jobpilot.agents.shortlist --job-id 42  # re-score one job
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
  Analytics" label and the company's "About" text are ignored. Roles with no AI/ML component are capped at 65.

On Gemini's free tier, Google may use what you send (your resume and the JDs) to improve its products.

## Documents agent (tailored resume + cover letter)

```powershell
.\.venv\Scripts\python -m jobpilot.agents.documents         # up to 10 shortlisted jobs without documents
.\.venv\Scripts\python -m jobpilot.agents.documents --job-id 111 # (re)generate for one job
```

For each shortlisted job it writes `output\<Company>_<id>\` with these files:
- `<Name>_Resume_<Company>.docx`
- `<Name>_Cover_Letter_<Company>.docx`
- `match_report.md`: JD keywords covered and missing, plus every fact-check fix

It then moves the job to **Docs ready**.

**Truthfulness.** The tailoring code lives in `jobpilot/tailoring`. Its prompt allows only facts from the master resume, and its fact check removes:
- skills, companies, institutions and certifications that aren't in the resume;
- bullets and sentences containing numbers that aren't in the resume.

JobPilot adds one more check:
- Every role's dates must match your resume's dates for that company. Dates that don't match are corrected and the fix is logged.
- A job title that isn't in your resume fails that job, so nothing is written for it.

Anything the JD asks for that your resume doesn't show is listed in the report and never added.

Your resume is `RESUME_PATH` in `.env`; scoring uses the same file. When you save a new version, point `RESUME_PATH` at it. The new content gets a new version hash, so documents and scores are regenerated instead of coming from the cache. Contact details come from the legacy `config\application_profile.yaml`. Review every document before you upload it.

## API (backend for the dashboard)

```powershell
powershell -ExecutionPolicy Bypass -File E:\JobPilot\scripts\run_api.ps1             # quiet; -Verbose logs every request
powershell -ExecutionPolicy Bypass -File E:\JobPilot\scripts\run_api.ps1 -Restart    # replace a running copy
```

It serves the built dashboard at http://127.0.0.1:8000 and interactive API docs at http://127.0.0.1:8000/docs. The API listens on 127.0.0.1 only, and it also refuses
any request that doesn't come from this computer.

- **Pipeline:** `GET /api/meta` (rail counts, applied today vs cap), plus `/api/jobs` with filters (`status`, `source`,
  `min_score`, `q` full-text search, `include_examples`), sorting and paging. `PATCH /api/jobs/{id}`
  changes status or notes, and every status change is written to history with actor `user`.
- **Agents:** `POST /api/runs/{search|score|docs|apply}` starts the agent as a background process and returns its run id.
  `GET /api/runs/{id}/stream` is a Server-Sent Events stream of its log. It tails the database, so runs started by
  the scheduler can be watched too. A second start of the same agent is refused while one is running.
  Each process's console output goes to `data\logs\run_<id>.log`.
- **Review queue:** `POST /api/applications/{id}/approve` or `/skip` only works while the application is waiting
  for review and an apply run is holding the form open; Approve is refused for a form that is not on its
  Submit page. The apply agent is the only thing that touches the browser.
- **Schedule:** `GET /api/schedule` (Task Scheduler status, log tail), `POST /api/schedule/register` and `/unregister`.
- **Other routes:** answers CRUD, settings (validated: threshold 30–95, daily cap 1–40), weekly review,
  analytics (`/api/analytics/funnel`, `/score-histogram`, `/applications-per-day`), and CSV/JSON export.

## Dashboard

Day to day it is served by the API at http://127.0.0.1:8000 from `web\dist`. Only while changing the UI:

```powershell
powershell -ExecutionPolicy Bypass -File E:\JobPilot\scripts\run_web.ps1   # Vite dev server with hot reload -> http://127.0.0.1:5173
cd E:\JobPilot\web; npm run build                                          # afterwards, so :8000 serves the new build
```

The dashboard is built with React, Vite, Tailwind and TypeScript. It uses the "Ledger" layout with the "Flight deck" palette and type, in light and dark mode (the header toggle cycles system → light → dark).

| Screen | What it does |
|---|---|
| **Pipeline rail** | Stage counts (click one to filter), a proportional bar, the threshold slider (saved when you release it), and applied today vs the cap |
| **Board** | Dense rows per stage. Drag a row to another column to change its status, and the move is written to history |
| **Jobs** | Sortable table: score, role, company, location, experience, source, rule score, status, found |
| **Job drawer** | Score and sub-score bars, the reason, matched skills, status, notes (saved when you leave the field), posting link, documents, JD, history, delete |
| **Review** | Filled fields with where each value came from (Gemini drafts and empty or consent fields highlighted), a screenshot, and Approve submit / Skip. Approve is locked while a form is stuck before its Submit page. For Naukri it says nothing has been sent yet (Apply is the submit). Blocked applications are listed separately |
| **Runs** | Run now for search, score, docs and apply (apply has a review-only option), with a live log over Server-Sent Events, plus run history with logs |
| **Answers** | Add, edit and delete screening answers; open questions the apply agent met are highlighted; the weekly review card |
| **Database** | Table cards with row counts, CSV/JSON export, and removing or adding the example rows |
| **Analytics** | Stat tiles (jobs tracked, shortlisted, applied, interviews), the pipeline funnel, the fit-score histogram with the threshold marked, and applications per day (30/90 days). Hover any bar for its value; every chart has a Table view; example rows are left out unless ticked |
| **Settings** | Threshold, daily cap, search queries, LinkedIn Easy-Apply-only, the auto-submit switch, review wait, weekly review time, the Windows schedule (status, Register / Update / Remove), Gemini model and key status (the key is never shown), and the site-terms warning |

Keyboard: `/` jumps to search, `N` adds a job, `Esc` closes the drawer.

## Apply agent (LinkedIn Easy Apply and Naukri)

```powershell
.venv\Scripts\python.exe -m jobpilot.agents.apply --review-only --limit 1   # fill one form, wait for you
.venv\Scripts\python.exe -m jobpilot.agents.apply --job-id 190 --review-only
.venv\Scripts\python.exe -m jobpilot.agents.apply.answers_import         # one-off: answer bank from config\application_profile.yaml
```

- Uses your **installed Chrome** with the signed-in profile (`data\browser_profile`), headed, one tab, a 20–90 s random pause between jobs, and the daily cap. It never types a password.
- Every field records its source: resume/profile, answer bank, or a Gemini draft (Gemini only answers from your resume and may leave a field empty). Unknown questions are added to the answer bank as open questions.
- **Consent, terms and privacy questions are never ticked or answered by the agent**, even if the answer bank has one.
- It stops on LinkedIn's Submit page, saves a screenshot, sends a Windows toast and waits (default 20 min) for Approve or Skip in the Review queue. If it couldn't reach the Submit page, the application is marked *incomplete*: finish it in the agent's Chrome window and either Approve (it unlocks once the form reaches Submit) or submit it there yourself; JobPilot records it either way.
- **Auto-submit** (Settings, off by default) submits without asking only when every field came from your resume/profile or answer bank, nothing is empty or consent-gated, and the job is Docs-ready with score ≥ threshold.
- CAPTCHA, checkpoint or login wall: the application is marked *blocked*, you get a toast, the window stays open and the run stops. Nothing tries to solve it.
- Jobs that are "Apply on company website" are remembered and never opened again; the drawer marks them *External apply*.
- **Naukri** works differently: its **Apply** button *is* the submit, and screening questions come afterwards in a chat.
  So the agent only reads the posting (signed in? external? already applied?), takes a screenshot and waits.
  On Approve (or auto-submit) it unticks Naukri's pre-ticked "Follow <company>" box, clicks Apply, and answers the
  chat **only from your answer bank**: Gemini never types into Naukri and consent questions are left to you. A question
  the bank can't answer stops it with the chat open for you, and becomes an open question on the Answers tab.
  "Login to apply" is treated as a login wall (blocked).
- Which flow a job gets is decided from its link (linkedin.com / naukri.com), not from the feed's label.
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
| `JobPilot Catch-up` | 2 min after sign-in, unlock or wake from sleep | `pythonw -m jobpilot.scheduled --catch-up`: a full pass only if the last search is overdue (one interval minus 30 min) and it is inside the window |

The tasks run only while you are logged on (search uses your signed-in browser profile), start a missed run when the PC is next available, never overlap (a lock file plus *ignore new instance*), stop after 2 h, and need no admin rights. Windows doesn't reliably start missed runs after sleep or modern
standby, hence the catch-up task; a run Windows starts more than 30 min after the window closes skips itself. **Apply is never scheduled**: it opens a browser and waits for you. After changing the times in Settings, click **Update tasks**.

## Tests

```powershell
.\.venv\Scripts\python -m pytest            # 106 tests, about 2.5 min
.\.venv\Scripts\python -m pytest tests\test_ui_smoke.py   # dashboard end to end (needs `npm run build` first)
```

- Database, dedupe, import, scorer, docs and API tests use a fresh database built by the real migrations; Gemini is faked.
- The apply tests run the real fillers against fake LinkedIn and Naukri pages in headless Chrome, and
  prove nothing is submitted without permission (Submit/Apply clicks are counted).
- The UI smoke test serves `web\dist` from the real API on a temporary seeded database and clicks through every
  screen in headless Chrome: moves a job, checks chart tooltips and tables, the theme toggle, no console errors,
  and no sideways scrolling at phone width.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `[Errno 10048] ... 8000` when starting the API | JobPilot is already running. Use the updated `run_api.ps1` (it says so), or add `-Restart` |
| http://127.0.0.1:8000 says `{"detail":"Not Found"}` | The running API is older than the build, or `web\dist` is missing: `npm run build` in `web`, then `run_api.ps1 -Restart` |
| A run says "Not signed in to linkedin/naukri" | Run `python -m jobpilot.agents.apply.login <portal>` in `E:\JobPilot` and sign in yourself |
| An application is *blocked* | LinkedIn or Naukri showed a CAPTCHA, OTP or login page. Deal with it in the agent's Chrome window; JobPilot never solves them |
| Scheduled runs don't appear | Settings -> Schedule shows the tasks and the last scheduled log; the PC must be on and you logged in |
| pip or npm fails with "no space" | C: is full; set `PIP_CACHE_DIR`, `npm_config_cache` and `TMP` to folders under `E:\JobPilot\.cache` |

