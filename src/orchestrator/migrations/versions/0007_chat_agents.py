"""chat sessions can run as a saved agent

Revision ID: 0007_chat_agents
Revises: 0006_standalone_runs
Create Date: 2026-11-02 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0007_chat_agents"
down_revision: str | None = "0006_standalone_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("chat_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("agent_name", sqlmodel.sql.sqltypes.AutoString()))


def downgrade() -> None:
    with op.batch_alter_table("chat_sessions", schema=None) as batch_op:
        batch_op.drop_column("agent_name")
