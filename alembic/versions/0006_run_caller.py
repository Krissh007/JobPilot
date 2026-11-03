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

COLUMNS = (("caller", sa.String(16)), ("auto_submit_requested", sa.Boolean), ("auto_submit_setting", sa.Boolean))


def upgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("agent_runs")}   # already there: nothing to add
    with op.batch_alter_table("agent_runs") as batch:
        for name, type_ in COLUMNS:
            if name not in have:
                batch.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_column("auto_submit_setting")
        batch.drop_column("auto_submit_requested")
        batch.drop_column("caller")
