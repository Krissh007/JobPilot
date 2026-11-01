"""agent_runs: who started the run and what auto-submit was asked for (all nullable, so older rows and DBs still load)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-01
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch:
        batch.add_column(sa.Column("caller", sa.String(16), nullable=True))
        batch.add_column(sa.Column("auto_submit_requested", sa.Boolean, nullable=True))
        batch.add_column(sa.Column("auto_submit_setting", sa.Boolean, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_column("auto_submit_setting")
        batch.drop_column("auto_submit_requested")
        batch.drop_column("caller")
