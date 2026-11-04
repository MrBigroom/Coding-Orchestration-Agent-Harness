"""persistent notification records for the event fan-out (T-9)

A desktop notification is best-effort, so every decision is also stored here: skipped because
the channel was off, attempted and delivered, or attempted and failed. Additive only.

Revision ID: 0010_notifications
Revises: 0009_chat_continue
Create Date: 2026-11-04 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0010_notifications"
down_revision: str | None = "0009_chat_continue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("trigger", sa.String(length=48), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("link", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("channel", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("dedupe_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("coalesce_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("coalesced_into", sa.Integer(), nullable=True),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_notifications_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_notifications_task_id"), ["task_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_notifications_run_id"), ["run_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_notifications_trigger"), ["trigger"], unique=False)
        batch_op.create_index(batch_op.f("ix_notifications_status"), ["status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_notifications_coalesce_key"), ["coalesce_key"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_notifications_dedupe_key"), ["dedupe_key"], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_notifications_dedupe_key"))
        batch_op.drop_index(batch_op.f("ix_notifications_coalesce_key"))
        batch_op.drop_index(batch_op.f("ix_notifications_status"))
        batch_op.drop_index(batch_op.f("ix_notifications_trigger"))
        batch_op.drop_index(batch_op.f("ix_notifications_run_id"))
        batch_op.drop_index(batch_op.f("ix_notifications_task_id"))
        batch_op.drop_index(batch_op.f("ix_notifications_created_at"))
    op.drop_table("notifications")
