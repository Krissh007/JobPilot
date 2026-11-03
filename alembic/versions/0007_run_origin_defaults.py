"""agent_runs: runs that predate the origin columns get caller 'unknown' and auto_submit_requested false

Only fills NULLs, so it is safe to repeat and never overwrites a recorded value. auto_submit_setting stays NULL on
older rows: the Settings switch back then was not recorded.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-03
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE agent_runs SET caller = 'unknown' WHERE caller IS NULL")
    op.execute("UPDATE agent_runs SET auto_submit_requested = 0 WHERE auto_submit_requested IS NULL")


def downgrade() -> None:
    pass   # the filled values are valid data; nothing to undo
