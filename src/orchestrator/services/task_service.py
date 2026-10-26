"""Task CRUD and state transitions. Every status change goes through `transition`."""

from __future__ import annotations

import uuid

from sqlmodel import Session, col, select

from orchestrator.core.models import (
    Run,
    Task,
    TaskKind,
    TaskSource,
    TaskStatus,
    utcnow,
)
from orchestrator.core.schemas import TaskCreate, TaskUpdate
from orchestrator.core.state import ACTIVE_RUN_STATES, InvalidTransition, check_task_transition
from orchestrator.services import log_service
from orchestrator.utils.slug import phase_branch


class TaskError(ValueError):
    pass


def phase_label(task: Task) -> str:
    if task.phase_number is not None:
        return f"phase-{task.phase_number}"
    return task.short_id


def create(session: Session, data: TaskCreate) -> Task:
    task = Task(
        short_id=f"tmp-{uuid.uuid4().hex}",
        project_id=data.project_id,
        parent_task_id=data.parent_task_id,
        phase_number=data.phase_number,
        phase_slug=data.phase_slug,
        title=data.title,
        description=data.description,
        kind=data.kind.value,
        risk=data.risk.value,
        priority=data.priority.value,
        tags=data.tags,
        acceptance_criteria=data.acceptance_criteria,
        definition_of_done=data.definition_of_done,
        frozen_contracts=data.frozen_contracts,
        out_of_scope=data.out_of_scope,
        assigned_provider_profile=data.assigned_provider_profile,
        assigned_harness_adapter=data.assigned_harness_adapter,
        source=data.source.value,
        created_by=data.created_by,
        agent_id=data.agent_id,
        status=TaskStatus.draft,
    )
    session.add(task)
    session.flush()
    task.short_id = f"T-{task.id}"
    if task.kind in (TaskKind.coding, TaskKind.maintenance) and task.project_id:
        task.branch_name = phase_branch(task.phase_number, task.phase_slug or task.title, task.id)
    session.add(task)
    session.commit()
    session.refresh(task)
    log_service.log_event(
        session,
        "task_created",
        f"{task.short_id}: {task.title}",
        task_id=task.id,
        phase=phase_label(task),
    )
    if data.queue:
        transition(session, task, TaskStatus.queued, "queued on creation")
    else:
        transition(session, task, TaskStatus.planned, "created")
    return task


def get(session: Session, ref: str | int) -> Task:
    task = None
    s = str(ref)
    if s.isdigit():
        task = session.get(Task, int(s))
    if task is None:
        task = session.exec(select(Task).where(Task.short_id == s.upper())).first()
    if not task:
        raise TaskError(f"task {ref!r} not found")
    return task


def list_tasks(
    session: Session,
    *,
    project_id: int | None = None,
    status: str | None = None,
    kind: str | None = None,
    risk: str | None = None,
    tag: str | None = None,
    include_archived: bool = False,
) -> list[Task]:
    q = select(Task)
    if project_id:
        q = q.where(Task.project_id == project_id)
    if status:
        q = q.where(Task.status == status)
    elif not include_archived:
        q = q.where(Task.status != TaskStatus.archived)
    if kind:
        q = q.where(Task.kind == kind)
    if risk:
        q = q.where(Task.risk == risk)
    tasks = list(session.exec(q.order_by(col(Task.id).desc())))
    if tag:
        tasks = [t for t in tasks if tag.lower() in t.tags]
    return tasks


def update(session: Session, task: Task, data: TaskUpdate) -> Task:
    for k, v in data.model_dump(exclude_unset=True).items():
        if k == "tags" and v is not None:
            v = sorted({t.strip().lower() for t in v if t.strip()})
        if hasattr(v, "value"):
            v = v.value
        setattr(task, k, v)
    task.updated_at = utcnow()
    session.add(task)
    session.commit()
    return task


def transition(
    session: Session, task: Task, dst: str, reason: str = "", role: str = "system"
) -> Task:
    src = task.status
    check_task_transition(src, dst)
    task.status = TaskStatus(dst).value
    task.updated_at = utcnow()
    session.add(task)
    session.commit()
    log_service.log_event(
        session,
        "task_status",
        f"{src} -> {dst}" + (f": {reason}" if reason else ""),
        role=role,
        task_id=task.id,
        phase=phase_label(task),
    )
    return task


def active_run(session: Session, task: Task) -> Run | None:
    return session.exec(
        select(Run).where(Run.task_id == task.id, col(Run.status).in_(ACTIVE_RUN_STATES))
    ).first()


def runs_for(session: Session, task: Task) -> list[Run]:
    return list(
        session.exec(select(Run).where(Run.task_id == task.id).order_by(col(Run.id).desc()))
    )


def latest_run(session: Session, task: Task) -> Run | None:
    runs = runs_for(session, task)
    return runs[0] if runs else None


def queue(session: Session, task: Task, source: str | None = None) -> Task:
    if source:
        task.source = TaskSource(source).value
    return transition(session, task, TaskStatus.queued, "queued")


def block(session: Session, task: Task, reason: str, role: str = "supervisor") -> Task:
    return transition(session, task, TaskStatus.blocked, reason or "blocked", role=role)


def unblock(session: Session, task: Task, reason: str = "") -> Task:
    if task.status != TaskStatus.blocked:
        raise InvalidTransition("task", task.status, "queued (unblock requires `blocked`)")
    return transition(
        session, task, TaskStatus.queued, reason or "unblocked by operator", role="supervisor"
    )


def archive(session: Session, task: Task) -> Task:
    if active_run(session, task):
        raise TaskError("cannot archive a task with an active run; cancel it first")
    return transition(session, task, TaskStatus.archived, "archived")
