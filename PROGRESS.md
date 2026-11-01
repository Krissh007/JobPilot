# JobPilot progress

Design: Phase 0 plan (approved 2026-10-06). It covers the folder structure, ERD, API routes, wireframes and the scheduler choice.

| Phase | Status | Notes |
|---|---|---|
| 0 Design | ✅ approved | Wrap `E:\job_search_agent`; separate `jobpilot.db`; JobPilot owns apply + scheduling |
| 1 DB + migrations + seed | ✅ done | See below |
| 2 Search integration | ✅ done | See below |
| 3 Scorer | ✅ done, verified with a real run | 149/150 real jobs scored with gemini-3.5-flash-lite + prompt v3 |
| 4 FastAPI + pytest | ✅ done, verified with a real run | 31 operations, live on the real DB |
| 5 Dashboard UI | ✅ done, verified with a real run | Direction: B layout + A colours (your pick) |
| 6 Docs generator | ✅ done, verified with a real run | Resume: your recent .docx · model gemini-3.5-flash-lite |
| 7 Auto-apply + review queue | ⏳ next | LinkedIn, then Naukri (pre-check → approve → apply) |
| 8 Scheduler | — | Windows Task Scheduler, 08–20 IST every 4 h |
| 9 Analytics + README | — | |

## Phase 1: database (2026-10-06)
- SQLModel models in `jobpilot/db/models.py`. The schema is owned by Alembic `0001_initial`.
- Enums are stored as VARCHAR with a CHECK constraint, so SQLite rejects unknown statuses.
- WAL, foreign keys, busy_timeout and synchronous=NORMAL are set on every connection (`jobpilot/db/session.py`).
- `jobs_fts` is an FTS5 table (porter stemming) over title, company and jd_text. Insert, update and delete triggers keep it in sync.
- Dedupe (`jobpilot/db/dedupe.py`) works in two steps:
  - Canonical URL: tracking params stripped, host lower-cased, LinkedIn and Naukri URLs collapsed to their job id.
  - Fallback: a hash of normalised company + title + city.
- `repo.upsert_job` refreshes listing fields but never touches status, notes or docs. `repo.set_status` always writes `status_history`.
- Default settings are inserted by the migration: threshold 60, daily cap 10, search queries, schedule.
- Seed data: 15 fictional example jobs covering every stage, plus 3 runs, 2 applications (1 pending_review, 1 blocked) and 6 answers. All are `is_example=1`; `--purge` removes them.
- Additions to the spec: `jobs.legacy_score`, `is_example` on runs/applications/answers, and the `llm_cache` table.
- Tests: `pytest` → 17 passed (dedupe cases, pragmas, settings, upsert/dedupe, history, enum CHECK, URL unique, FTS triggers, seed/purge).

## Phase 2: search integration (2026-10-06)
- Legacy edit (the one you approved): `E:\job_search_agent\config\settings.yaml` → `auto_apply.enabled: false`. Nothing else in that repo was touched.
- `jobpilot/legacy/legacy_shim.py` runs under the legacy Python with `cwd=E:\job_search_agent`. For that run only, it turns off tailoring and `prepare_after_search` in memory and calls the unchanged `discovery_pipeline.run()`.
- `jobpilot/legacy/importer.py` opens the legacy `jobs.db` with `mode=ro`, falling back to the newest `new_leads_*.xlsx`.
  - Legacy times are IST and are converted to UTC.
  - Statuses are mapped only on first insert. Rows the legacy gates rejected are skipped.
- `jobpilot/agents/_run.py` writes the agent_runs row as soon as a run starts. It flushes the log every 2 s, and an error or Ctrl+C is still recorded.
- `python -m jobpilot.agents.search [--import-only] [--keywords ...] [--sources ...]`.
- Dedupe fix: the company+title+city fallback now applies only **across portals**. Before the fix, the first real import merged 10 separate Accenture "Business Analyst, Hyderabad" openings into one job.
- Real import: 179 legacy rows → 138 new, 1 merged (same job under two URLs), 40 skipped as legacy-rejected. Running it again adds 0 new.
- Real browser search (one keyword, Naukri + LinkedIn, about 5 min): legacy discovered 120 → 13 matched → JobPilot imported 12 new (1 was already present). Exit 0, agent_run #6 ok.
- Tests: 25 passed. They cover IST conversion, status map, Excel fallback, read-only DB, idempotent import that keeps JobPilot status, run recorder ok/error, and the import-only end-to-end run.

## Phase 3: scorer (2026-10-06)
- Resume: the legacy `config/master_resume.md` (your choice for now). Override it with `RESUME_PATH`. `resume_version` is a sha256 of the resume content (first 12 hex chars).
- `jobpilot/llm/`:
  - Token bucket (`GEMINI_RPM`).
  - stdlib REST client: 429/5xx backoff with jitter that honours `retryDelay`. "Thought" parts are dropped.
  - Errors: `QuotaExhausted` (still 429 after retries) and `LLMConfigError` (missing key or model, HTTP 400/401/403/404).
  - Cache in `llm_cache`, keyed by sha256(prompt version + JD + resume version).
- `jobpilot/agents/score.py`:
  - Pydantic `ScoreResult` (score/skills/experience/domain 0-100, reason, matched/missing skills), with one retry that spells out the validation error.
  - Commits one job at a time. Auto-moves found → shortlisted (actor = agent).
  - Picks the best legacy score first. Skips jobs with a JD under 150 characters, and examples.
- `search --then-score` runs search and then score, for the scheduler.
- Bug found by the real run: a missing key was counted per job as "bad output" and the run ended `ok`. Config errors now stop the run as `error` (run #8).
- Real queue: 150 unscored real jobs, 149 of them with a usable JD.
- Tests: 41 passed, with Gemini mocked: client fake + HTTP opener fake, backoff timing, cache hit, resume change, quota stop, config stop.

### Phase 3 real verification (2026-10-06, after the key was added)
- Real calls work: 3 jobs scored with gemini-3.8-flash. The cache hit and the resume-change re-score both behaved as expected (a temporary resume copy was used; the real resume was not touched).
- Problems the real runs found, all fixed:
  1. **Generic BA roles scored 85–92.** The v2 prompt makes domain favour AI roles. v3 judges domain only from the responsibilities: flash-lite had been crediting Naukri's "Department: Data Science & Analytics" label and the company blurb. Roles with no AI component are capped at 70.
  2. **gemini-3.8-flash allows only 20 requests/day on the free tier.** The client retried that limit for 46 minutes. Daily-quota 429s now fail in one call (the quota id and reset time are in the message), with optional `GEMINI_FALLBACK_MODELS`.
  3. **Backoff was invisible.** Retries now appear in the agent_runs log.
- Your decision: scoring uses `GEMINI_MODEL=gemini-3.5-flash-lite` (only that line in .env was changed). The cache was cleared so all scores come from one model and the v3 prompt.
- Tests: 43 passed.
- Real backfill (run #16): 146 jobs in 18 min, 0 failures, 0 retries. Result: 45 shortlisted, 100 found, 5 applied. 1 job has no usable JD.
  Score histogram: 10s:4, 20s:12, 30s:9, 40s:16, 50s:59, 60s:16, 70s:24, 80s:4, 90s:5. 19 jobs are AI-centred (domain ≥ 75).
  Spot-checked top scores (RSA, Sutherland "Agentic AI BA", Amazon, Infor): the AI work is in the responsibilities themselves.

## Phase 4: FastAPI (2026-10-06)
- `jobpilot/api/`: `create_app(db_url)` and 7 routers (data/meta, jobs, applications, runs, answers, settings, analytics). There are 31 operations.
  Requests from other machines are refused. CORS allows the Vite dev server (localhost:5173).
- Run now: `POST /api/runs/{agent}` creates the run row first, then starts `python -m jobpilot.agents.<agent> --run-id N`
  as a hidden subprocess. Options are whitelisted, so nothing from a request reaches a shell.
  - A second start of a running agent is refused (409). A "running" row older than 2 h with no live process doesn't block.
  - A process that dies without recording an outcome is marked `error`, with a pointer to its console log.
- SSE: `GET /api/runs/{id}/stream` polls the run's log in the DB every second. It sends `log` events, then `end`, with a keep-alive every 15 s.
- Deferred to Phase 7: `POST /jobs/{id}/queue-apply`, because its states depend on the apply agent's design.
- Bugs found and fixed:
  1. `?state=` was ignored on `/api/applications`. FastAPI treated the list as a request body.
  2. The SSE stream dropped the agent's first line. The agent overwrote the API's "Starting..." log instead of appending.
  3. Funnel off by one. My prompt-tuning resets had left shortlisted→found churn in history for jobs #74, #79 and #87; it was removed.
- Real run (server on 127.0.0.1:8000, real DB):
  - Meta: 165 jobs (15 examples).
  - Top shortlisted: RSA 96, Infor 90, Sutherland 90.
  - Search "agentic" returns 11 hits.
  - Real-only funnel: 150 → 50 → 5 → 5. Export: 165 rows.
  - Runs #17 (score) and #18 (search import) were started through the API and streamed to `end: ok`. A double start returned 409. Apply returns 501 until Phase 7.
- Tests: 60 passed, including a real subprocess run streamed end to end, a crashed process marked error, and the remote-client 403.

## One database (2026-10-06, your request)
- `jobpilot.db` is now the only database the pipeline writes. The shim calls the legacy `discover_jobs` and `score_jobs` (legacy matching and gates) directly, and streams matches as `JOBPILOT_JOB {json}` lines. The legacy save, tailor, prepare, export and project_sync stages are no longer called.
- JobPilot passes the URLs it already has, so known jobs are skipped during discovery. Legacy debug page dumps go to `E:\JobPilot\data\debug`.
- The old legacy `jobs.db` is a read-only archive. It was imported once in Phase 2, and `--import-legacy-db` can copy from it again.
- Real run, "AI Product Analyst", Naukri + LinkedIn:
  - 165 known URLs skipped. Discovered 77 → 7 stale → 65 rejected by the legacy gates → 5 matched.
  - Result: 4 new jobs plus 1 cross-portal duplicate.
  - The legacy project's `jobs.db` (md5), exports, `project_sync` and logs are byte-identical before and after.
  - Scored right after (run #21): Techdome 90, LPL Data Product Analyst 85 and Wise 75 shortlisted; Infosys 35.
- Also on your request: the old exposed key file `E:\job_search_agent\.env.txt` was moved to the Recycle Bin. JobPilot's `.env` stays.
- Tests: 61 passed. A new test drives the stdout protocol through a fake shim and checks that nothing is written into the legacy folder.

## Phase 5: dashboard UI (2026-10-06)
- Directions: `design/direction-a-flight-deck.html` and `design/direction-b-ledger.html`. You picked a mix: **B's layout** (dense rows, proportional pipeline bar, segmented filters, keyboard hints) with **A's colours and type** (teal on blue-grey, Bricolage Grotesque + IBM Plex).
- `web/`: React 19, Vite 8, Tailwind 4, TypeScript and React Query. Every colour is a CSS variable, with light, dark and system themes.
  - Screens: rail, Board (drag-and-drop), Jobs table, job drawer, Add job, Review queue, Runs (Run now + SSE live log), Answers, Database, Settings. Analytics shows a placeholder until Phase 9.
- New API routes: `GET /api/stats/tables` (row counts) and `GET /api/system` (non-secret config; the key is reported only as set or not set, and a test checks it never appears).
- The search agent now takes its queries from the Settings screen (`settings.search_queries`), so they live in one place.
- Real-browser verification (headless Chromium against the live API and the real DB; anything that changes data used example rows only, and the examples were reset afterwards):
  - Drag found→shortlisted persisted with actor `user`. Full-text search for "agentic" returned the Sutherland Agentic AI BA.
  - Notes autosave, and the drawer's status select, both persisted.
  - Adding a duplicate URL opened the existing job instead of creating a copy.
  - The threshold slider saved 61 and was restored to 60.
  - Approve in the review queue worked.
  - Run now (score run #22) streamed its log live to `finished`.
  - Answer bank add, edit and delete worked.
  - No console errors. No horizontal page scroll at 390 px phone width.
- Bugs found in the screenshots and fixed: 7 board columns overflowed at 1440 px; the "up to N jobs" input stretched full width (a `w-full` class conflict); the Jobs "Score ↓" header wrapped.
- `npm run build`: 314 kB JS (94 kB gzipped). Screenshots in light and dark: `design/screens/`.
- Tests: 62 pytest passed. The Playwright smoke test is in Phase 9, as planned.


## Phase 6: docs generator (2026-10-06)
- Prerequisites (your answers):
  - Resume: `E:\Krishna BA Resume\Krishna_Maheswaram_AI_Business_Analyst_Resume_Naukri.docx`. It's the most recent general version (29 Sep 13:58) with the corrected dates (LogNormal Oct 2025–Sep 2026, Softic Feb 2024–Sep 2025, NZX Feb 2022–Feb 2024). The other 12 versions were compared; the Infor ones are tailored to one job and older ones have the old dates.
  - The content is identical to the `master_resume.md` used for scoring (similarity 1.0), so existing scores stand.
  - `RESUME_PATH` was added to `.env` (one line; the key wasn't read or touched). Model: `gemini-3.5-flash-lite`.
- `jobpilot/legacy/bridge.py` imports the legacy `src.tailoring` (prompt, fact check, `.docx` writer, report) unchanged. `resume.py` reads `.docx`/`.pdf` with the legacy reader, so scoring and tailoring see the same text.
- `jobpilot/agents/docs.py`:
  - Selection: shortlisted, non-example jobs with a JD ≥ 300 chars and no documents yet, best score first.
  - Output goes to `output/<Company>_<id>/`. The raw model JSON is cached; the checks re-run every time.
  - New date/title check, because the legacy fact check didn't verify either: dates are corrected from your resume, and an unknown title fails the job so nothing is written.
  - Moves the job shortlisted → docs_ready (actor agent). "Run now" for docs is enabled in the API and the Runs screen, and the drawer has Resume, Cover, Report and Open folder.
- Real run #26: Techdome, Infor and Sutherland, about 6 s each.
  - Every date in the 3 resumes equals your corrected dates, titles are verbatim, and the cover letters contain no invented numbers.
  - ATS keyword coverage: Infor 93%, Techdome 43%, Sutherland 36%. The gaps (BPMN, SIPOC, n8n, LangChain…) are listed in each report, not invented.
  - The fact check removed "Process Mapping" from Sutherland's skills (not in your resume).
  - Run #27 (Run now from the API) regenerated Sutherland from the cache with no Gemini call.
- Notes:
  - RSA America (96) was skipped because you moved it to Applied from the dashboard at 15:43 IST.
  - flash-lite's cover letters run about 215 words, under the legacy prompt's 250–350 target.
- Tests: 68 passed. New: real legacy tailoring with a fake Gemini, covering invented skill and number removed, old dates corrected, unknown title writes nothing, cache, company-name variants, and selection.


## Phase 7: apply agent + review queue (2026-10-08)
- Your rules:
  - Auto-submit only when every field came from your resume/profile or the answer bank, the job is Docs-ready and scores at least the threshold. A Gemini draft, an empty field or a consent box sends it to Review.
  - The Settings switch is **OFF** until you've checked a real run.
  - Weekly answer review: Mondays 09:00 IST (dashboard banner + Windows toast via `python -m jobpilot.weekly`; scheduled in Phase 8).
- Sign-in: LinkedIn is signed in, in the persistent profile (`li_at` cookie present). Naukri isn't signed in yet, so the Naukri adapter waits.
- Answer bank: imported from the legacy `application_profile.yaml`: 6 answers + 5 open questions for you (expected CTC, relocation, work authorization ×2, years of experience).
- `jobpilot/agents/apply/`:
  - The legacy `EasyApplyFiller` is subclassed so every field records its source (resume / answer bank / Gemini draft).
  - Headed installed Chrome, one tab, 20–90 s random pauses, daily cap.
  - CAPTCHA, checkpoint or login wall: the application is marked blocked, you get a toast, and the run stops.
  - Not-Easy-Apply jobs are remembered (`applications.reason`, migration 0003) and never opened again.
- Search: a new **LinkedIn Easy Apply only** setting (on) adds LinkedIn's own `f_AL=true` filter.
- The first live test found all 11 Docs-ready LinkedIn jobs were "Apply on company website". The drawer now says "External apply" for those.
- Real review-mode run #44 (job #190 HighRadius, score 58):
  - Filled 6 fields (5 resume/profile, 1 answer bank) and took a screenshot. Nothing was submitted; the 20-minute wait timed out.
  - The form was actually stuck on page 4/5 on a **required data-consent checkbox**, but the log said "stopped before Submit". Fixed:
    - The agent never ticks, chooses or answers consent/terms/privacy questions, even if the answer bank has one. They're listed as "(not ticked: only you can agree to this)".
    - A form that isn't on LinkedIn's Submit page is marked `incomplete`. The log says "form NOT ready: stuck on step N … Needs you: …".
    - For an incomplete form, Approve is refused by the API (409) and disabled in Review, with an amber note.
    - If you finish the steps yourself in the agent's window, the agent notices the Submit page and unlocks Approve. If you submit there yourself, it records the application.
- Tests: 84 passed. The new apply tests run a fake LinkedIn form in headless Chrome and cover:
  - auto-submit, draft → review → approve, review-only never submits
  - open question, consent box never ticked + Approve refused until you finish
  - security block, not signed in, daily cap, broken page, not-Easy-Apply remembered
- Still open: the 5 open answers (yours); Naukri after you sign in; turning auto-submit on (your call).


## Phase 8: scheduler (2026-10-08)
- `jobpilot/scheduled.py` runs one pass: search → score → docs.
  - Every step runs even if an earlier one failed, and each step is its own row in Agent runs.
  - A lock file stops overlapping passes; a lock left by a crashed pass is taken over.
  - Under `pythonw` it logs to `data\logs\scheduled.log`. The legacy search no longer opens a console window.
  - Apply is never scheduled.
- `scripts/register_tasks.ps1` (`-WhatIf`, `-Unregister`) registers two tasks for your user, using the times in Settings:
  - `JobPilot Search-Score-Docs`: daily 08:00, every 4 h for 14 h → 08:00, 12:00, 16:00, 20:00 IST.
  - `JobPilot Weekly-Answer-Review`: Monday 09:00 → `jobpilot.weekly` toast.
  - Both are Interactive logon (only while you're logged on), start a missed run when the PC is available, ignore a new instance while one runs, stop after 2 h, and need no admin.
- API: `GET /api/schedule` returns the Task Scheduler status (cached 20 s) and the log tail. `POST /api/schedule/register` and `/unregister` run the same script.
- Dashboard: Settings → **Schedule** panel:
  - Edit every/from/until, preview the run times.
  - Task status with next run, last run and result, plus Register / Update / Remove.
  - The terminal command and the last scheduled log.
- **Registered on your request** at 08:00 IST. Next pipeline run: today 12:00. Next weekly reminder: Mon 12 Oct 09:00.
- Real pass run by hand (`python -m jobpilot.scheduled --log`, 07:54–08:07, exit 0):
  - Search: 265 discovered, 108 stale, 140 rejected by gates, **17 new**.
  - Score: 7 shortlisted (Deloitte 93, Techdome 92, Infor 85, MassMutual, JPMorgan, Wise, CGI 62).
  - Docs: **8 new Docs-ready** jobs.
  - 2 jobs skipped:
    - Micro1 #29 hit Word's 255-char keywords limit. Fixed: the legacy writer's property setter is wrapped in memory and the keywords cut to fit. On the retry the cover letter was too short after the fact check, which is a correct skip.
    - Micro1 #47: the same short-cover-letter skip.
- Tests: 90 passed. New:
  - pipeline continues after a failed step; lock skip and stale-lock takeover
  - config from Settings
  - schedule API with PowerShell mocked (status, register, unregister, Task Scheduler error)
  - Word keyword cap
- Screens: `design/screens/13-settings-schedule-*.png`, `14-schedule-registered-*.png`.


## Phase 7b: Naukri apply (2026-10-08)
- You signed in to Naukri with the legacy helper. Checked headless in the agent profile, cookie names only: `nauk_at`/`nauk_rt` present (Naukri) and `li_at` present (LinkedIn).
- `jobpilot/agents/apply/naukri.py` reuses the legacy `NaukriApplier` (its selectors, chatbot handling and confirmation check), split in two:
  - `precheck()` only reads the posting: signed in? external? already applied? It never clicks. On Naukri, Apply IS the submit.
  - `submit()` clicks Apply and answers the chatbot **only from your answer bank/profile**. Gemini drafts are never typed into Naukri, and consent/terms questions are left for you.
  - An unknown question stops it, the chat stays open for you, and the question becomes an open question in the answer bank.
- Runner:
  - Docs-ready Naukri jobs ≥ threshold are now queued alongside LinkedIn.
  - Per-portal sign-in check: a signed-out portal's jobs are skipped with the login command.
  - "Login to apply" counts as a login wall → blocked.
  - Company-site jobs → reason `external`, never retried.
  - Same auto-submit rule as LinkedIn (setting on + Docs-ready + score ≥ threshold).
- Dashboard: Queue for apply works on Naukri jobs. The Review card explains that nothing has been sent yet and that Approve clicks Naukri's Apply. Wording updated in Settings and Runs.
- Bug found by the new tests: the legacy matcher wants lower-cased keys, so bank answers weren't used in the Naukri chat. Fixed.
- Scheduler: a run Windows starts late is now skipped when it's past the window + 30 min.
  - Found tonight: the PC slept through 12:00, 16:00 and 20:00, and Windows started the missed run at 23:49.
  - Manual runs aren't affected.
- Tests: 98 passed. New Naukri tests:
  - nothing is clicked while waiting; approve → applied
  - auto-submit with bank answers; unknown chat question handed to you + open question
  - consent never answered; company site external + not retried
  - login prompt → blocked; signed-out portal skipped with the hint
  - plus the late-run window test
- Not yet done: a live Naukri review-mode run. It needs you at the PC to Approve or Skip within 20 min.

### Live Naukri test (2026-10-09 00:01 IST, run #51)
- Deloitte Shared Services India, Business Analyst (#197, score 93), review mode:
  - Posting checked, screenshot taken, nothing sent.
  - You approved at 00:21:06. The agent clicked Apply.
  - The chat asked "years of experience as Business analyst?" and was answered from your answer bank ("3 years BA, Overall 6+ years.").
  - Naukri confirmed, and the job is **Applied** (actor: you).
- Fixed after the test:
  - The chat's closing "Thank you for your responses." was treated as an unanswerable question. That added a junk open question and flagged the application incomplete for 4 s. A bot line with no box or options is now just a message; the agent waits for the confirmation.
  - A submitted application drops the `incomplete` flag.
  - Naukri pre-ticks "Follow <company> as you apply". The agent now unticks it (best effort) before Apply, and Review shows what it did.
  - The legacy feed mislabelled 2 jobs (#132 Infor said naukri with a LinkedIn link; #210 the reverse). The portal is now taken from the link when importing and when choosing the apply flow, and both rows were relabelled.
- Tests: 100 passed.


## Phase 9: analytics + UI smoke test + README (2026-10-10 to 2026-10-11, done)
- Analytics tab:
  - Stat tiles; pipeline funnel; fit-score histogram (threshold marker, at/above vs below); applications per day (30/90 days).
  - Hover tooltips, keyboard focus and a table view on every chart; examples excluded unless ticked.
  - Chart colours validated with the dataviz validator in both themes (`--chart`, `--chart-dim`).
- The API now serves the built dashboard (`web/dist`) at http://127.0.0.1:8000. Windows MIME types for .js/.css are pinned (the registry mapped .js to text/plain).
- `tests/test_ui_smoke.py`: headless Chrome against the real API and a temp seeded DB. Clicks every screen, moves a job, checks tooltip/table and theme, no console errors, no sideways scroll at 390 px.
- Bugs it found and fixed:
  - The drawer status toast said "Moved to Found" (the stale select value).
  - Settings scrolled sideways on phones (grid children need min-w-0).
- Tests: 106 passed. Screens: `design/screens/15-analytics-*.png`.
- `run_api.ps1`: says JobPilot is already running instead of a port-in-use crash, `-Restart` replaces the running copy, quiet by default (warnings and errors only), `-Verbose` logs every request.
- README rewritten around daily use and first-time setup, with Naukri, the catch-up task, tests and troubleshooting. New `docs/DESIGN.md`: architecture, data model, lifecycle, apply flow and safety rules, scheduling, testing, known limits.

## Status: all 9 phases done
Still yours to do: answer the open questions on the Answers tab, and decide when to switch on auto-submit.


## Plan: search criteria, then auto-apply fixes (2026-10-25)

### A. Search criteria (done in code, awaiting your confirmation of the title list)
- `discovery.strict_titles` (default on): portals are searched only for the keywords, and a card is saved only if its title contains a keyword, a registered variant or a related title (`RELATED_TITLES`). Everything else is counted as `off-title` and never saved.
- Settings `search_queries` now holds 16 titles drawn from the resume: AI Business Analyst, AI Product Analyst, Business Analyst, Product Analyst, Associate Product Manager, Product Owner, Business Systems Analyst, Functional Analyst, Technical Business Analyst, Business Intelligence Analyst, Data Analyst, Product Analytics, AI Business Consultant, Analytics Consultant, Insights Analyst, AI Solutions Analyst.
- Open: how to treat your AI-assisted building work (JARVIS, the job agent). It is on the resume as projects; plain Developer/Engineer titles stay excluded. Tell me if you want AI-builder roles added.
- Open: remaining filter changes (auto-reject below threshold, experience 3-5 instead of 3-7) are not made.

### B. Auto-apply fixes (starts once A is final). Evidence from the live DB, 25 Oct
- 42 real applications: 4 submitted, 38 skipped. Skips: not_easy_apply 13, superseded 8, external 6, timeout 4, draft_saved 3, tab_closed 2, browser_closed 2.
- Readiness: 2 of 20 clean reviews decided, so auto-submit stays OFF (verdict "NOT YET").
- docs_ready queue: 47 LinkedIn, 93 Naukri. Last apply run was 15 Oct; Apply is never scheduled.
1. Stop queuing jobs that cannot be applied to: skip `external` / `not_easy_apply` before opening them (LinkedIn Easy Apply filter at search, remember the verdict on the job).
2. Parked forms: finish the 3 `draft_saved` applications and add the missing answers (5 open questions on the Answers tab).
3. Review queue robustness: timeouts, closed tab/browser and `superseded` should not lose the filled form; resume instead of restarting.
4. Naukri: first live auto run with chat answers from the answer bank only.
5. Re-run readiness after 20 clean reviews; only then decide on auto-submit.
