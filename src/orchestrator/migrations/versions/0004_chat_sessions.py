"""interactive harness sessions

Revision ID: 0004_chat_sessions
Revises: 0003_sync
Create Date: 2026-11-01 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0004_chat_sessions"
down_revision: str | None = "0003_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("adapter", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("model", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("effort", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("permission", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("workspace", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("cwd", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("branch_name", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("chat_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_chat_sessions_project_id"), ["project_id"])
        batch_op.create_index(batch_op.f("ix_chat_sessions_status"), ["status"])


def downgrade() -> None:
    with op.batch_alter_table("chat_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_chat_sessions_status"))
        batch_op.drop_index(batch_op.f("ix_chat_sessions_project_id"))
    op.drop_table("chat_sessions")
