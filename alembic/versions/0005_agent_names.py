"""agent_runs.agent: allow the four redesign agent names (discovery, scoring, documents, apply)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

OLD = ("search", "score", "docs", "apply")
NEW = ("discovery", "scoring", "documents", "apply", "search", "score", "docs")


def _in(values) -> str:
    return "agent IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    with op.batch_alter_table("agent_runs", recreate="always") as batch:
        batch.alter_column("agent", existing_type=sa.String(6), type_=sa.String(9), existing_nullable=False)
        batch.drop_constraint("agent_name", type_="check")
        batch.create_check_constraint("agent_name", _in(NEW))


def downgrade() -> None:
    op.execute("DELETE FROM agent_runs WHERE agent IN ('discovery', 'scoring', 'documents')")
    with op.batch_alter_table("agent_runs", recreate="always") as batch:
        batch.drop_constraint("agent_name", type_="check")
        batch.create_check_constraint("agent_name", _in(OLD))
        batch.alter_column("agent", existing_type=sa.String(9), type_=sa.String(6), existing_nullable=False)
