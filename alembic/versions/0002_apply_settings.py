"""apply-agent and weekly-review settings

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07
"""
import json

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

NEW_SETTINGS = {
    "auto_submit": False,
    "review_wait_minutes": 20,
    "weekly_review": {"day": "mon", "time": "09:00"},
    "answers_reviewed_at": None,
}


def upgrade() -> None:
    bind = op.get_bind()
    for key, value in NEW_SETTINGS.items():
        bind.execute(sa.text("INSERT OR IGNORE INTO settings (key, value) VALUES (:k, :v)"),
                     {"k": key, "v": json.dumps(value)})


def downgrade() -> None:
    bind = op.get_bind()
    for key in NEW_SETTINGS:
        bind.execute(sa.text("DELETE FROM settings WHERE key = :k"), {"k": key})
