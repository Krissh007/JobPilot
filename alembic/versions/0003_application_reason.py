"""applications.reason; linkedin_easy_apply_only setting

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07
"""
import json

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("applications") as batch:
        batch.add_column(sa.Column("reason", sa.String, nullable=False, server_default=""))
    bind = op.get_bind()
    # Every real application skipped so far was a job LinkedIn sends to the company site.
    bind.execute(sa.text("UPDATE applications SET reason = 'not_easy_apply' "
                         "WHERE state = 'skipped' AND screenshot_path = '' AND is_example = 0"))
    bind.execute(sa.text("INSERT OR IGNORE INTO settings (key, value) VALUES ('linkedin_easy_apply_only', :v)"),
                 {"v": json.dumps(True)})


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM settings WHERE key = 'linkedin_easy_apply_only'"))
    with op.batch_alter_table("applications") as batch:
        batch.drop_column("reason")
