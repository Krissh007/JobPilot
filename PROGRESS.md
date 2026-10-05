# JobPilot progress

Design: Phase 0 plan (approved 2026-10-06). It covers the folder structure, ERD, API routes, wireframes and the scheduler choice.

| Phase | Status | Notes |
|---|---|---|
| 0 Design | ✅ approved | Wrap `E:\job_search_agent`; separate `jobpilot.db`; JobPilot owns apply + scheduling |
| 1 DB + migrations + seed | ✅ done | See below |
| 2 Search integration | ✅ done | See below |
| 3 Scorer | ✅ done (mocked) | Real run needs `GEMINI_API_KEY` + `GEMINI_MODEL` in `.env` |
| 4 FastAPI + pytest | ⏳ next | |
| 5 Dashboard UI | — | 2 visual directions first |
| 6 Docs generator | — | Reuses legacy `src/tailoring` |
| 7 Auto-apply + review queue | — | LinkedIn, then Naukri (pre-check → approve → apply) |
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
