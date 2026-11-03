"""model context windows, and per-call usage events for the Usage page

Revision ID: 0008_usage_events
Revises: 0007_chat_agents
Create Date: 2026-11-03 12:00:00.000000

Existing runs and chat sessions only kept running totals, so the backfill files each one's
total under the day it started.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "0008_usage_events"
down_revision: str | None = "0007_chat_agents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("model_profiles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("context_window", sa.Integer(), nullable=True))
    events = op.create_table(
        "usage_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("via", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("model", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("usage_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_usage_events_created_at"), ["created_at"])
    op.bulk_insert(events, list(_backfill(op.get_bind())))


def _backfill(conn: sa.Connection):
    def row(when, source, via, model, usage):
        if isinstance(usage, str):
            usage = json.loads(usage or "{}")
        usage = usage or {}
        if not usage.get("input_tokens") and not usage.get("output_tokens"):
            return None
        if isinstance(when, str):
            when = datetime.fromisoformat(when)
        return {
            "created_at": when or datetime.now(UTC),
            "source": source,
            "via": via,
            "model": model,
            "input_tokens": usage.get("input_tokens") or 0,
            "cached_input_tokens": usage.get("cache_read_input_tokens") or 0,
            "output_tokens": usage.get("output_tokens") or 0,
            "requests": usage.get("requests") or 1,
        }

    runs = conn.execute(
        sa.text(
            "SELECT r.usage, r.harness_adapter, r.started_at, r.created_at,"
            " m.model_name, p.name FROM runs r"
            " LEFT JOIN model_profiles m ON m.name = r.provider_profile"
            " LEFT JOIN providers p ON p.id = m.provider_id"
        )
    )
    for usage, harness, started, created, profile_model, provider in runs:
        parsed = json.loads(usage) if isinstance(usage, str) else (usage or {})
        if parsed.get("source") == "harness":
            r = row(started or created, "task", harness, parsed.get("model"), parsed)
        else:
            r = row(started or created, "api", provider, profile_model, parsed)
        if r:
            yield r
    chats = conn.execute(
        sa.text("SELECT usage, adapter, model, standalone, created_at FROM chat_sessions")
    )
    for usage, adapter, model, standalone, created in chats:
        if r := row(created, "standalone" if standalone else "chat", adapter, model, usage):
            yield r


def downgrade() -> None:
    with op.batch_alter_table("usage_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_usage_events_created_at"))
    op.drop_table("usage_events")
    with op.batch_alter_table("model_profiles", schema=None) as batch_op:
        batch_op.drop_column("context_window")
