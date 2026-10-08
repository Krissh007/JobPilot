"""initial schema: jobs, history, runs, applications, answers, settings, llm cache, FTS5

Revision ID: 0001
Revises:
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa

from jobpilot.db.models import (
    Actor,
    AgentName,
    ApplicationState,
    FieldSource,
    JobStatus,
    RunStatus,
)

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def enum(cls, name):
    return sa.Enum(cls, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("company", sa.String, nullable=False),
        sa.Column("source", sa.String, nullable=False, server_default=""),
        sa.Column("url", sa.String, nullable=False, unique=True),
        sa.Column("url_canonical", sa.String, nullable=False),
        sa.Column("dedupe_hash", sa.String, nullable=False),
        sa.Column("location", sa.String, nullable=False, server_default=""),
        sa.Column("exp_min", sa.Integer),
        sa.Column("exp_max", sa.Integer),
        sa.Column("jd_text", sa.Text, nullable=False, server_default=""),
        sa.Column("skills", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("score", sa.Float),
        sa.Column("score_skills", sa.Float),
        sa.Column("score_experience", sa.Float),
        sa.Column("score_domain", sa.Float),
        sa.Column("score_reason", sa.Text, nullable=False, server_default=""),
        sa.Column("legacy_score", sa.Float),
        sa.Column("status", enum(JobStatus, "job_status"), nullable=False, server_default="found"),
        sa.Column("resume_version", sa.String, nullable=False, server_default=""),
        sa.Column("resume_path", sa.String, nullable=False, server_default=""),
        sa.Column("cover_letter_path", sa.String, nullable=False, server_default=""),
        sa.Column("found_at", sa.DateTime, nullable=False),
        sa.Column("applied_at", sa.DateTime),
        sa.Column("notes", sa.Text, nullable=False, server_default=""),
        sa.Column("is_example", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_score", "jobs", ["score"])
    op.create_index("ix_jobs_found_at", "jobs", ["found_at"])
    op.create_index("ix_jobs_url_canonical", "jobs", ["url_canonical"])
    op.create_index("ix_jobs_dedupe_hash", "jobs", ["dedupe_hash"])

    op.create_table(
        "status_history",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_status", enum(JobStatus, "history_from_status")),
        sa.Column("to_status", enum(JobStatus, "history_to_status"), nullable=False),
        sa.Column("changed_at", sa.DateTime, nullable=False),
        sa.Column("actor", enum(Actor, "history_actor"), nullable=False),
    )
    op.create_index("ix_status_history_job_id", "status_history", ["job_id"])

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("agent", enum(AgentName, "agent_name"), nullable=False),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("finished_at", sa.DateTime),
        sa.Column("found", sa.Integer, nullable=False, server_default="0"),
        sa.Column("processed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", enum(RunStatus, "run_status"), nullable=False),
        sa.Column("error", sa.Text, nullable=False, server_default=""),
        sa.Column("log", sa.Text, nullable=False, server_default=""),
        sa.Column("is_example", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_agent_runs_agent", "agent_runs", ["agent"])

    op.create_table(
        "applications",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state", enum(ApplicationState, "application_state"), nullable=False),
        sa.Column("screenshot_path", sa.String, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("decided_at", sa.DateTime),
        sa.Column("is_example", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_applications_job_id", "applications", ["job_id"])
    op.create_index("ix_applications_state", "applications", ["state"])

    op.create_table(
        "application_fields",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("application_id", sa.Integer, sa.ForeignKey("applications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step", sa.Integer, nullable=False, server_default="1"),
        sa.Column("field_name", sa.String, nullable=False),
        sa.Column("value_used", sa.Text, nullable=False, server_default=""),
        sa.Column("source", enum(FieldSource, "field_source"), nullable=False),
        sa.Column("needs_review", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_application_fields_application_id", "application_fields", ["application_id"])

    op.create_table(
        "answers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("answer", sa.Text, nullable=False),
        sa.Column("tags", sa.String, nullable=False, server_default=""),
        sa.Column("times_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("is_example", sa.Boolean, nullable=False, server_default=sa.false()),
    )

    settings = op.create_table(
        "settings",
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
    )
    # Frozen copy of the defaults at 0001 (later settings arrive in later migrations).
    op.bulk_insert(settings, [
        {"key": "threshold", "value": 60},
        {"key": "daily_apply_cap", "value": 10},
        {"key": "search_queries", "value": ["AI Business Analyst", "AI Product Analyst", "Business Analyst", "Product Analyst"]},
        {"key": "schedule", "value": {"every_hours": 4, "start": "08:00", "end": "22:00", "timezone": "Asia/Kolkata"}},
    ])

    op.create_table(
        "llm_cache",
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("response", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    # Full-text search over JDs, kept in sync with jobs by triggers.
    op.execute(
        "CREATE VIRTUAL TABLE jobs_fts USING fts5("
        "title, company, jd_text, content='jobs', content_rowid='id', tokenize='porter unicode61')"
    )
    op.execute(
        "CREATE TRIGGER jobs_fts_ai AFTER INSERT ON jobs BEGIN "
        "INSERT INTO jobs_fts(rowid, title, company, jd_text) VALUES (new.id, new.title, new.company, new.jd_text); END"
    )
    op.execute(
        "CREATE TRIGGER jobs_fts_ad AFTER DELETE ON jobs BEGIN "
        "INSERT INTO jobs_fts(jobs_fts, rowid, title, company, jd_text) "
        "VALUES ('delete', old.id, old.title, old.company, old.jd_text); END"
    )
    op.execute(
        "CREATE TRIGGER jobs_fts_au AFTER UPDATE OF title, company, jd_text ON jobs BEGIN "
        "INSERT INTO jobs_fts(jobs_fts, rowid, title, company, jd_text) "
        "VALUES ('delete', old.id, old.title, old.company, old.jd_text); "
        "INSERT INTO jobs_fts(rowid, title, company, jd_text) VALUES (new.id, new.title, new.company, new.jd_text); END"
    )


def downgrade() -> None:
    for trigger in ("jobs_fts_ai", "jobs_fts_ad", "jobs_fts_au"):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute("DROP TABLE IF EXISTS jobs_fts")
    for table in ("llm_cache", "settings", "answers", "application_fields", "applications",
                  "agent_runs", "status_history", "jobs"):
        op.drop_table(table)
