"""jobs.posted_on: the portal's posting date, so the enrichment backlog can expire stale jobs

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-14
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.add_column(sa.Column("posted_on", sa.Date, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("posted_on")
