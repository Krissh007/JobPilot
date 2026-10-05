# JobPilot progress

Design: Phase 0 plan (approved 2026-10-06). It covers the folder structure, ERD, API routes, wireframes and the scheduler choice.

| Phase | Status | Notes |
|---|---|---|
| 0 Design | ✅ approved | Wrap `E:\job_search_agent`; separate `jobpilot.db`; JobPilot owns apply + scheduling |
| 1 DB + migrations + seed | ✅ done | See below |
| 2 Search integration | ⏳ next | Legacy shim + importer; set legacy `auto_apply.enabled: false` |
| 3 Scorer | — | Needs the master resume `.docx` path |
| 4 FastAPI + pytest | — | |
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
