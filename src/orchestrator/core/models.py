"""SQLModel table definitions. This module is a frozen contract after baseline approval:
any change here requires a contract-change request and a new Alembic revision."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, String, Text
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- enums


class TaskKind(StrEnum):
    coding = "coding"
    agent = "agent"
    review = "review"
    maintenance = "maintenance"


class TaskStatus(StrEnum):
    draft = "draft"
    planned = "planned"
    queued = "queued"
    blocked = "blocked"
    in_progress = "in_progress"
    review = "review"
    accepted = "accepted"
    rejected = "rejected"
    archived = "archived"


class TaskRisk(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class TaskPriority(StrEnum):
    low = "low"
    normal = "normal"
    high = "high"


class TaskSource(StrEnum):
    manual = "manual"
    schedule = "schedule"
    escalation = "escalation"
    retry = "retry"


class RunStatus(StrEnum):
    pending = "pending"
    preparing = "preparing"
    running = "running"
    testing = "testing"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    timeout = "timeout"


class ReviewVerdict(StrEnum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class ProviderKind(StrEnum):
    mock = "mock"
    anthropic = "anthropic"
    openai_compatible = "openai_compatible"
    claude_cli = "claude_cli"


class Role(StrEnum):
    architect = "architect"
    supervisor = "supervisor"
    worker = "worker"
    reviewer = "reviewer"
    agent = "agent"


class MemoryCategory(StrEnum):
    global_ = "global"
    project = "project"
    role = "role"


def _json(default: Any) -> Any:
    return Field(
        default_factory=lambda: default.copy() if hasattr(default, "copy") else default,
        sa_column=Column(JSON, nullable=False),
    )


# ---------------------------------------------------------------- tables


class Project(SQLModel, table=True):
    __tablename__ = "projects"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    slug: str = Field(index=True, unique=True)
    path: str
    base_branch: str = "main"
    test_command: str | None = None
    lint_command: str | None = None
    build_command: str | None = None
    allowed_commands: list[str] = _json([])
    notes: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Task(SQLModel, table=True):
    __tablename__ = "tasks"

    id: int | None = Field(default=None, primary_key=True)
    short_id: str = Field(index=True, unique=True)
    project_id: int | None = Field(default=None, foreign_key="projects.id", index=True)
    parent_task_id: int | None = Field(default=None, foreign_key="tasks.id")
    phase_number: int | None = None
    phase_slug: str | None = None
    title: str
    description: str = Field(default="", sa_column=Column(Text, nullable=False))
    kind: str = Field(default=TaskKind.coding, sa_column=Column(String(32), nullable=False))
    status: str = Field(
        default=TaskStatus.draft, sa_column=Column(String(32), nullable=False, index=True)
    )
    risk: str = Field(default=TaskRisk.low, sa_column=Column(String(16), nullable=False))
    priority: str = Field(default=TaskPriority.normal, sa_column=Column(String(16), nullable=False))
    tags: list[str] = _json([])
    acceptance_criteria: list[str] = _json([])
    definition_of_done: list[str] = _json([])
    frozen_contracts: list[str] = _json([])
    out_of_scope: list[str] = _json([])
    branch_name: str | None = None
    assigned_provider_profile: str | None = None
    assigned_harness_adapter: str | None = None
    handoff_status: str | None = None
    review_status: str | None = None
    verification_status: str | None = None
    merge_status: str | None = None
    created_by: str = "user"
    source: str = Field(default=TaskSource.manual, sa_column=Column(String(16), nullable=False))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Run(SQLModel, table=True):
    __tablename__ = "runs"

    id: int | None = Field(default=None, primary_key=True)
    short_id: str = Field(index=True, unique=True)
    task_id: int = Field(foreign_key="tasks.id", index=True)
    status: str = Field(
        default=RunStatus.pending, sa_column=Column(String(16), nullable=False, index=True)
    )
    provider_profile: str | None = None
    harness_adapter: str | None = None
    command_preview: str | None = Field(default=None, sa_column=Column(Text))
    worktree_path: str | None = None
    branch_name: str | None = None
    commit_sha: str | None = None
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    error: str | None = Field(default=None, sa_column=Column(Text))
    usage: dict[str, Any] = _json({})
    artifacts_path: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Review(SQLModel, table=True):
    __tablename__ = "reviews"

    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="tasks.id", index=True)
    run_id: int | None = Field(default=None, foreign_key="runs.id")
    reviewer_kind: str = "architect"
    reviewer_model: str | None = None
    verdict: str = Field(sa_column=Column(String(32), nullable=False))
    findings: list[dict[str, Any]] = _json([])
    reasoning: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow)


class Verification(SQLModel, table=True):
    __tablename__ = "verifications"

    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="tasks.id", index=True)
    run_id: int | None = Field(default=None, foreign_key="runs.id")
    verifier_kind: str = "supervisor"
    status: str = Field(sa_column=Column(String(16), nullable=False))
    checks_json: list[dict[str, Any]] = _json([])
    evidence_path: str | None = None
    contradicts_worker: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class Schedule(SQLModel, table=True):
    __tablename__ = "schedules"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    cron_expression: str
    task_template_json: dict[str, Any] = _json({})
    enabled: bool = True
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Provider(SQLModel, table=True):
    __tablename__ = "providers"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    kind: str = Field(sa_column=Column(String(32), nullable=False))
    base_url: str | None = None
    # Name of the env var that holds the key — never the key itself.
    api_key_env: str | None = None
    enabled: bool = True
    notes: str | None = Field(default=None, sa_column=Column(Text))


class ModelProfile(SQLModel, table=True):
    __tablename__ = "model_profiles"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    role: str = Field(sa_column=Column(String(32), nullable=False))
    provider_id: int = Field(foreign_key="providers.id")
    model_name: str
    # Ignored for Anthropic Claude 5.x models (sampling params were removed there).
    temperature: float | None = None
    max_tokens: int = 16_000
    timeout_seconds: int = 600
    # Anthropic output_config.effort (low|medium|high|xhigh|max); ignored by other providers.
    effort: str | None = None
    enabled: bool = True
    notes: str | None = Field(default=None, sa_column=Column(Text))


class HarnessAdapter(SQLModel, table=True):
    __tablename__ = "harness_adapters"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    kind: str = Field(sa_column=Column(String(32), nullable=False))
    command: str
    args_template_json: list[str] = _json([])
    # Names of env vars to pass through to the child process (values are read at run time).
    env_vars_json: list[str] = _json([])
    timeout_seconds: int = 1800
    enabled: bool = True
    capabilities_json: dict[str, Any] = _json({})
    notes: str | None = Field(default=None, sa_column=Column(Text))


class MemoryEntry(SQLModel, table=True):
    __tablename__ = "memory_entries"

    id: int | None = Field(default=None, primary_key=True)
    category: str = Field(sa_column=Column(String(16), nullable=False, index=True))
    scope: str = Field(index=True)
    project_slug: str | None = Field(default=None, index=True)
    role: str | None = Field(default=None, index=True)
    content: str = Field(sa_column=Column(Text, nullable=False))
    source: str = "manual"
    tags_json: list[str] = _json([])
    approved: bool = Field(default=False, index=True)
    # Set when a suggestion is explicitly rejected, so it no longer shows as pending.
    rejected: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class BuildLogEntry(SQLModel, table=True):
    __tablename__ = "build_log_entries"

    id: int | None = Field(default=None, primary_key=True)
    timestamp: datetime = Field(default_factory=utcnow, index=True)
    phase: str | None = None
    task_id: int | None = Field(default=None, index=True)
    run_id: int | None = None
    role: str = "system"
    event: str
    details: str = Field(default="", sa_column=Column(Text, nullable=False))
    commit_sha: str | None = None


class Setting(SQLModel, table=True):
    __tablename__ = "settings"

    key: str = Field(primary_key=True)
    value_json: Any = Field(default=None, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=utcnow)
