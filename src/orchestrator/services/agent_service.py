"""Saved agents: reusable instructions + model (+ optional project and harness).

Running an agent creates a task. With a project it is a coding task, so it goes through the
normal worker path: isolated worktree, the harness routed for the agent's model, the
Supervisor's independent verification and the Architect review. Without a project it is an
agent-kind task answered by the model directly (no repo to edit, so no harness)."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, col, select

from orchestrator.core.models import (
    Agent,
    Provider,
    Role,
    Schedule,
    Task,
    TaskKind,
    TaskRisk,
    TaskSource,
    utcnow,
)
from orchestrator.core.schemas import AgentCreate, AgentRun, AgentUpdate, TaskCreate
from orchestrator.services import (
    adapter_service,
    log_service,
    project_service,
    provider_service,
    task_service,
)


class AgentError(ValueError):
    pass


def _check_refs(
    session: Session, model_profile: str | None, harness: str | None, project_id: int | None
) -> None:
    if model_profile:
        provider_service.get_profile(session, model_profile)
    if harness:
        adapter_service.get_row(session, harness)
    if project_id:
        project_service.get(session, project_id)


def create(session: Session, data: AgentCreate) -> Agent:
    if session.exec(select(Agent).where(Agent.name == data.name)).first():
        raise AgentError(f"agent {data.name!r} already exists")
    _check_refs(session, data.model_profile, data.harness_adapter, data.project_id)
    agent = Agent(**data.model_dump())
    session.add(agent)
    session.commit()
    session.refresh(agent)
    log_service.log_event(session, "agent_created", agent.name)
    return agent


def update(session: Session, agent: Agent, data: AgentUpdate) -> Agent:
    values = data.model_dump(exclude_unset=True)
    for key in ("model_profile", "harness_adapter"):
        if values.get(key) == "":
            values[key] = None
    if values.get("project_id") == 0:
        values["project_id"] = None
    _check_refs(
        session,
        values.get("model_profile"),
        values.get("harness_adapter"),
        values.get("project_id"),
    )
    for k, v in values.items():
        setattr(agent, k, v)
    agent.updated_at = utcnow()
    session.add(agent)
    session.commit()
    session.refresh(agent)
    return agent


def get(session: Session, ref: str | int) -> Agent:
    agent = None
    if str(ref).isdigit():
        agent = session.get(Agent, int(ref))
    if agent is None:
        agent = session.exec(select(Agent).where(Agent.name == str(ref))).first()
    if not agent:
        raise KeyError(f"agent {ref!r} not found")
    return agent


def list_agents(session: Session) -> list[Agent]:
    return list(session.exec(select(Agent).order_by(col(Agent.name))))


def delete(session: Session, agent: Agent) -> None:
    scheds = list(session.exec(select(Schedule).where(Schedule.agent_id == agent.id)))
    if scheds:
        names = ", ".join(s.name for s in scheds)
        raise AgentError(f"agent {agent.name!r} is used by schedule(s): {names}")
    session.delete(agent)
    session.commit()


def task_data(
    agent: Agent,
    run: AgentRun,
    *,
    source: TaskSource = TaskSource.manual,
    created_by: str | None = None,
) -> TaskCreate:
    """The TaskCreate an agent run turns into. Instructions go first so they stay in the
    prompt's static (cached) prefix across runs of the same agent."""
    first_line = run.request.strip().splitlines()[0][:80]
    parts = []
    if agent.instructions.strip():
        parts.append(f"Agent instructions ({agent.name}):\n{agent.instructions.strip()}")
    parts.append(f"Request:\n{run.request.strip()}")
    return TaskCreate(
        title=run.title or f"[{agent.name}] {first_line}",
        project_id=agent.project_id,
        description="\n\n".join(parts),
        kind=TaskKind.coding if agent.project_id else TaskKind.agent,
        risk=run.risk,
        tags=[*agent.tags, f"agent:{agent.name}"],
        definition_of_done=run.definition_of_done,
        assigned_provider_profile=agent.model_profile,
        assigned_harness_adapter=agent.harness_adapter,
        source=source,
        created_by=created_by or f"agent:{agent.name}",
        agent_id=agent.id,
        queue=run.queue,
    )


def run(
    session: Session,
    agent: Agent,
    data: AgentRun,
    *,
    source: TaskSource = TaskSource.manual,
    created_by: str | None = None,
) -> Task:
    if not agent.enabled:
        raise AgentError(f"agent {agent.name!r} is disabled")
    task = task_service.create(
        session, task_data(agent, data, source=source, created_by=created_by)
    )
    log_service.log_event(session, "agent_run", f"{agent.name} -> {task.short_id}", task_id=task.id)
    return task


def routing(session: Session, agent: Agent) -> dict[str, Any]:
    """Which model profile and harness an agent's run would use right now (dry run)."""
    draft = _draft(agent)
    role = Role.agent if draft.kind == TaskKind.agent else Role.worker
    try:
        choice = provider_service.choose_profile(session, draft, role)
    except provider_service.ProviderError as e:
        return {"profile": None, "profile_reason": str(e), "harness": None, "harness_reason": ""}
    out: dict[str, Any] = {
        "profile": choice.profile.name,
        "model": choice.profile.model_name,
        "profile_reason": choice.reason,
        "token_plan": uses_token_plan(session, choice.profile.name),
        "harness": None,
        "harness_reason": "no project: answered by the model directly (no harness)",
    }
    if draft.kind != TaskKind.agent:
        h = adapter_service.choose_harness(
            session, draft, choice.profile.name, choice.profile.model_name
        )
        out["harness"], out["harness_reason"] = h.adapter, h.reason
    return out


def _draft(agent: Agent) -> Task:
    """An unsaved Task with the routing-relevant fields of an agent run."""
    data = task_data(agent, AgentRun(request="(dry run)", queue=False))
    return Task(
        short_id="draft",
        title=data.title,
        kind=data.kind.value,
        project_id=data.project_id,
        risk=TaskRisk.low,
        tags=data.tags,
        assigned_provider_profile=data.assigned_provider_profile,
        assigned_harness_adapter=data.assigned_harness_adapter,
        source=TaskSource.manual,
    )


def uses_token_plan(session: Session, profile_name: str | None) -> bool:
    if not profile_name:
        return False
    try:
        profile = provider_service.get_profile(session, profile_name)
    except KeyError:
        return False
    prov = session.get(Provider, profile.provider_id)
    return bool(prov and (prov.name == "qwen-token-plan" or "token-plan" in (prov.base_url or "")))
