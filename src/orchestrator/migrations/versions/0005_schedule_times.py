"""one-time schedules and per-schedule time zones

Revision ID: 0005_schedule_times
Revises: 0004_chat_sessions
Create Date: 2026-11-01 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0005_schedule_times"
down_revision: str | None = "0004_chat_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.add_column(sa.Column("run_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "timezone",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="UTC",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.drop_column("timezone")
        batch_op.drop_column("run_at")
