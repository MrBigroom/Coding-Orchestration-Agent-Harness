"""saved agents; tasks.agent_id and schedules.agent_id

Revision ID: 0002_agents
Revises: 0001_baseline
Create Date: 2026-10-26 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_agents"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("model_profile", sa.String(), nullable=True),
        sa.Column("harness_adapter", sa.String(), nullable=True),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_agents_name"), ["name"], unique=True)

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("agent_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_tasks_agent_id"), ["agent_id"], unique=False)

    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.add_column(sa.Column("agent_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_schedules_agent_id"), ["agent_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_schedules_agent_id"))
        batch_op.drop_column("agent_id")

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tasks_agent_id"))
        batch_op.drop_column("agent_id")

    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_agents_name"))

    op.drop_table("agents")
