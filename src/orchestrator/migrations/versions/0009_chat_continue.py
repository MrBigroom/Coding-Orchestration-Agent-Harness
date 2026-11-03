"""chat sessions remember the harness's conversation id, so they can be continued

Revision ID: 0009_chat_continue
Revises: 0008_usage_events
Create Date: 2026-11-03 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0009_chat_continue"
down_revision: str | None = "0008_usage_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("chat_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("harness_session", sqlmodel.sql.sqltypes.AutoString()))


def downgrade() -> None:
    with op.batch_alter_table("chat_sessions", schema=None) as batch_op:
        batch_op.drop_column("harness_session")
