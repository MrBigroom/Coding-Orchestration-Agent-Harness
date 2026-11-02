"""standalone agent runs

Revision ID: 0006_standalone_runs
Revises: 0005_schedule_times
Create Date: 2026-11-02 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0006_standalone_runs"
down_revision: str | None = "0005_schedule_times"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "run_mode",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="pipeline",
            )
        )
        batch_op.add_column(
            sa.Column(
                "access",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="read",
            )
        )
        batch_op.add_column(
            sa.Column("time_limit_minutes", sa.Integer(), nullable=False, server_default="30")
        )
    with op.batch_alter_table("chat_sessions", schema=None) as batch_op:
        batch_op.alter_column("project_id", existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(
            sa.Column("standalone", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.create_table(
        "standalone_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("schedule_id", sa.Integer(), nullable=True),
        sa.Column("chat_id", sa.Integer(), nullable=True),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("access", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("adapter", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("model", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("report_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column("blocked", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("standalone_runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_standalone_runs_agent_id"), ["agent_id"])
        batch_op.create_index(batch_op.f("ix_standalone_runs_schedule_id"), ["schedule_id"])
        batch_op.create_index(batch_op.f("ix_standalone_runs_status"), ["status"])


def downgrade() -> None:
    with op.batch_alter_table("standalone_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_standalone_runs_status"))
        batch_op.drop_index(batch_op.f("ix_standalone_runs_schedule_id"))
        batch_op.drop_index(batch_op.f("ix_standalone_runs_agent_id"))
    op.drop_table("standalone_runs")
    op.execute("DELETE FROM chat_sessions WHERE project_id IS NULL")
    with op.batch_alter_table("chat_sessions", schema=None) as batch_op:
        batch_op.drop_column("standalone")
        batch_op.alter_column("project_id", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.drop_column("time_limit_minutes")
        batch_op.drop_column("access")
        batch_op.drop_column("run_mode")
