"""Pydantic schemas: role artifacts (handoff/review/verification), build log entries,
and API request/response bodies. Artifact schemas are frozen contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestrator.core.memory_vocab import check_role_name
from orchestrator.core.models import (
    MemoryCategory,
    ProviderKind,
    ReviewVerdict,
    Role,
    TaskKind,
    TaskPriority,
    TaskRisk,
    TaskSource,
)

# ---------------------------------------------------------------- artifacts


class HandoffTests(BaseModel):
    command: str = ""
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    log_path: str = ""


class DoDItem(BaseModel):
    item: str
    met: bool
    evidence: str = ""


class NewDependency(BaseModel):
    name: str
    version: str = ""
    reason: str = ""


class MemorySuggestion(BaseModel):
    category: Literal["global", "project", "role"]
    scope: str
    content: str
    tags: list[str] = Field(default_factory=list)


class Handoff(BaseModel):
    phase: str
    branch: str
    status: Literal["READY_FOR_REVIEW", "CHANGES_APPLIED", "BLOCKED"]
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    tests: HandoffTests = Field(default_factory=HandoffTests)
    dod_self_check: list[DoDItem] = Field(default_factory=list)
    new_dependencies: list[NewDependency] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    contracts_touched: list[str] = Field(default_factory=list)
    memory_suggestions: list[MemorySuggestion] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    file_or_area: str
    issue: str
    violates: str = ""


class ReviewResult(BaseModel):
    verdict: ReviewVerdict
    findings: list[ReviewFinding] = Field(default_factory=list)
    missing_artifacts: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class VerificationCheck(BaseModel):
    name: str
    # The frozen vocabulary (docs/frozen-contracts.md). `stale` is the fifth, additive value,
    # approved by the operator as CCR-T15-01 (2026-11-08): T-15's evidence-freshness check uses it
    # for a bundle that was compared and is out of date, so a machine consumer never has to parse
    # prose to tell "not certified" from "compared and stale". Existing values keep their meaning.
    status: Literal["pass", "fail", "skipped", "not_applicable", "stale"]
    command: str = ""
    evidence: str = ""
    notes: str = ""


class VerificationResult(BaseModel):
    phase: str
    checks: list[VerificationCheck] = Field(default_factory=list)
    contradicts_worker: bool = False
    final_result: Literal["pass", "fail", "needs_human"]


class BuildLogRecord(BaseModel):
    timestamp: str
    phase: str | None = None
    task_id: int | None = None
    run_id: int | None = None
    role: Literal["supervisor", "worker", "architect", "system"] = "system"
    event: str
    details: str = ""
    commit_sha: str | None = None


class PlanPhase(BaseModel):
    """Architect planning output for one phase (used by planning_service)."""

    phase_number: int
    phase_slug: str
    title: str
    description: str = ""
    definition_of_done: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    risk: TaskRisk = TaskRisk.low


class Plan(BaseModel):
    phases: list[PlanPhase]


# ---------------------------------------------------------------- API bodies


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProjectCreate(BaseModel):
    name: str
    path: str
    base_branch: str | None = None
    test_command: str | None = None
    lint_command: str | None = None
    build_command: str | None = None
    allowed_commands: list[str] = Field(default_factory=list)
    notes: str | None = None


class ProjectUpdate(BaseModel):
    path: str | None = None
    base_branch: str | None = None
    test_command: str | None = None
    lint_command: str | None = None
    build_command: str | None = None
    allowed_commands: list[str] | None = None
    notes: str | None = None


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    project_id: int | None = None
    description: str = ""
    kind: TaskKind = TaskKind.coding
    risk: TaskRisk = TaskRisk.low
    priority: TaskPriority = TaskPriority.normal
    tags: list[str] = Field(default_factory=list)
    phase_number: int | None = None
    phase_slug: str | None = None
    parent_task_id: int | None = None
    acceptance_criteria: list[str] = Field(default_factory=list)
    definition_of_done: list[str] = Field(default_factory=list)
    frozen_contracts: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    assigned_provider_profile: str | None = None
    assigned_harness_adapter: str | None = None
    source: TaskSource = TaskSource.manual
    created_by: str = "user"
    agent_id: int | None = None
    queue: bool = False

    @field_validator("tags")
    @classmethod
    def _norm_tags(cls, v: list[str]) -> list[str]:
        return sorted({t.strip().lower() for t in v if t.strip()})


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    risk: TaskRisk | None = None
    priority: TaskPriority | None = None
    tags: list[str] | None = None
    acceptance_criteria: list[str] | None = None
    definition_of_done: list[str] | None = None
    frozen_contracts: list[str] | None = None
    out_of_scope: list[str] | None = None
    assigned_provider_profile: str | None = None
    assigned_harness_adapter: str | None = None


class RetryRequest(BaseModel):
    provider_profile: str | None = None
    harness_adapter: str | None = None


class DecisionRequest(BaseModel):
    reason: str = ""


class ProviderCreate(BaseModel):
    name: str
    kind: ProviderKind
    base_url: str | None = None
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Z_][A-Z0-9_]*$")
    enabled: bool = True
    notes: str | None = None


class ProviderUpdate(BaseModel):
    base_url: str | None = None
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Z_][A-Z0-9_]*$")
    enabled: bool | None = None
    notes: str | None = None


class ModelProfileCreate(BaseModel):
    name: str
    role: Role
    provider_id: int
    model_name: str
    temperature: float | None = None
    max_tokens: int = 16_000
    timeout_seconds: int = 600
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    context_window: int | None = Field(default=None, gt=0)
    enabled: bool = True
    notes: str | None = None


class ModelProfileUpdate(BaseModel):
    role: Role | None = None
    provider_id: int | None = None
    model_name: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout_seconds: int | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    context_window: int | None = Field(default=None, gt=0)
    enabled: bool | None = None
    notes: str | None = None


class AdapterUpdate(BaseModel):
    command: str | None = None
    args_template_json: list[str] | None = None
    env_vars_json: list[str] | None = None
    timeout_seconds: int | None = None
    enabled: bool | None = None
    notes: str | None = None

    @field_validator("env_vars_json")
    @classmethod
    def _env_names(cls, v: list[str] | None) -> list[str] | None:
        import re

        if v is not None:
            for name in v:
                if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
                    raise ValueError(f"invalid env var name: {name!r}")
        return v


class ScheduleCreate(BaseModel):
    name: str
    # Set exactly one of these. A run_at without an offset is read in `timezone`.
    cron_expression: str = ""
    run_at: datetime | None = None
    timezone: str = "UTC"
    task_template_json: dict[str, Any] = Field(default_factory=dict)
    agent_id: int | None = None
    run_mode: Literal["pipeline", "standalone"] = "pipeline"
    access: Literal["read", "write"] = "read"
    time_limit_minutes: int = Field(default=30, ge=1, le=1440)
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    name: str | None = None
    cron_expression: str | None = None
    run_at: datetime | None = None
    timezone: str | None = None
    task_template_json: dict[str, Any] | None = None
    # 0 detaches the schedule from its agent.
    agent_id: int | None = None
    run_mode: Literal["pipeline", "standalone"] | None = None
    access: Literal["read", "write"] | None = None
    time_limit_minutes: int | None = Field(default=None, ge=1, le=1440)
    enabled: bool | None = None


def _norm_tag_list(v: list[str]) -> list[str]:
    return sorted({t.strip().lower() for t in v if t.strip()})


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")
    description: str = ""
    instructions: str = ""
    model_profile: str | None = None
    harness_adapter: str | None = None
    project_id: int | None = None
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return _norm_tag_list(v)


class AgentUpdate(BaseModel):
    description: str | None = None
    instructions: str | None = None
    # Empty string clears model/harness (back to automatic routing); 0 clears the project.
    model_profile: str | None = None
    harness_adapter: str | None = None
    project_id: int | None = None
    tags: list[str] | None = None
    enabled: bool | None = None

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _norm_tag_list(v)


class AgentRun(BaseModel):
    request: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=300)
    risk: TaskRisk = TaskRisk.low
    definition_of_done: list[str] = Field(default_factory=list)
    queue: bool = True


class MemoryCreate(BaseModel):
    """Request body for `POST /api/memory`.

    Two boundary checks matter for the memory vocabularies (T-4 DoD 2). `scope` keeps its
    shape pattern here and its MEMBERSHIP check in the service (`_validate`), because the
    allowed set depends on `category`, which a field validator cannot see. `role` is refused
    here, in `_role_names_the_memory_files`, because the `Role` enum would otherwise answer a
    bad value with its own list and the service would never be reached.
    """

    category: MemoryCategory
    scope: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    content: str = Field(min_length=1)
    project_slug: str | None = None
    role: Role | None = None
    tags: list[str] = Field(default_factory=list)
    source: str = "manual"
    approved: bool = False

    @model_validator(mode="before")
    @classmethod
    def _role_names_the_memory_files(cls, data: Any) -> Any:
        """Refuse an unreadable `role` with the role MEMORY FILES, before the enum speaks.

        `role` is the column `for_prompt` selects on, so it decides which prompt reads the
        row — the more damaging half of a bad row (T-4). Left to Pydantic, `POST /api/memory`
        with `role="logbook-writer"` is a 422 whose only words are `Input should be
        'architect', 'supervisor', 'worker', 'reviewer' or 'agent'`: the enum, without the
        `reviewer -> architect` / `agent -> worker` aliases that make those extra names legal,
        and without the vocabulary the operator has to fix the row against (a per-value grep
        for `architect|worker|supervisor` passes on that enum message too, which is exactly why
        the API test asserts the joined vocabulary and the words only `check_role_name` says).
        DoD 2 asks for the allowed VALUES at EVERY boundary, so this validator runs
        `check_role_name` — the same function `_validate`, `import_all` and the CLI's `--role`
        use — and the 422 detail carries that message.

        The accepted set is UNCHANGED: every value that passed before still passes (the five
        Role members, aliases included) and every value that failed still fails, so this is an
        error-TEXT improvement, not a widening of the frozen request shape — `role` stays
        typed as `Role`, so the OpenAPI enum and the response shape are untouched. `None` and
        an already-converted `Role` are left for the field validator; a `model_construct`
        payload (which skips validators) is still refused by `_validate` in the service.
        """
        if isinstance(data, dict):
            raw = data.get("role")
            if isinstance(raw, str) and not isinstance(raw, Role):
                check_role_name(raw)
        return data


class MemoryUpdate(BaseModel):
    content: str | None = None
    scope: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    tags: list[str] | None = None


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class PromptPreviewRequest(BaseModel):
    role: Role = Role.worker
    task_id: int | None = None
    project_slug: str | None = None
    # Optional explicit section order to check (used to test ordering warnings).
    sections: list[dict[str, Any]] | None = None


class ErrorBody(BaseModel):
    error: str
    detail: Any = None


class TimestampedOut(ORMModel):
    created_at: datetime | None = None


class ChatSessionCreate(BaseModel):
    project_id: int | None = None  # empty: the session gets its own empty folder
    agent_id: int | None = None  # its instructions go ahead of the first message
    adapter: str
    model: str | None = None
    effort: str | None = None
    permission: str | None = None
    workspace: Literal["worktree", "checkout"] = "worktree"
