"""local-first cloud synchronization metadata

Revision ID: 0003_sync
Revises: 0002_agents
Create Date: 2026-10-28 11:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_sync"
down_revision: str | None = "0002_agents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_entities",
        sa.Column("uuid", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("local_id", sa.String(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("synced_hash", sa.String(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("synced_version", sa.Integer(), nullable=False),
        sa.Column("dirty", sa.Boolean(), nullable=False),
        sa.Column("origin_device", sa.String(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("conflict_json", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("uuid"),
    )
    with op.batch_alter_table("sync_entities", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sync_entities_entity_type"), ["entity_type"], unique=False)
        batch_op.create_index(
            "ix_sync_entities_entity_type_local_id",
            ["entity_type", "local_id"],
            unique=True,
        )

    op.create_table(
        "sync_state",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("sync_state")
    with op.batch_alter_table("sync_entities", schema=None) as batch_op:
        batch_op.drop_index("ix_sync_entities_entity_type_local_id")
        batch_op.drop_index(batch_op.f("ix_sync_entities_entity_type"))
    op.drop_table("sync_entities")
