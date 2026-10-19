"""SQLite-backed task queue with atomic claims and parallelism limits."""

from __future__ import annotations

from sqlalchemy import case, update
from sqlmodel import Session, col, func, select

from orchestrator.core.models import Run, Task, TaskStatus, utcnow
from orchestrator.core.state import ACTIVE_RUN_STATES
from orchestrator.services import log_service, settings_service

_PRIORITY_ORDER = case(
    (col(Task.priority) == "high", 0), (col(Task.priority) == "normal", 1), else_=2
)


def active_run_count(session: Session, project_id: int | None = None) -> int:
    q = select(func.count()).select_from(Run).where(col(Run.status).in_(ACTIVE_RUN_STATES))
    if project_id is not None:
        q = q.join(Task, col(Task.id) == col(Run.task_id)).where(Task.project_id == project_id)
    return int(session.exec(q).one())


def in_progress_count(session: Session, project_id: int | None = None) -> int:
    q = select(func.count()).select_from(Task).where(Task.status == TaskStatus.in_progress)
    if project_id is not None:
        q = q.where(Task.project_id == project_id)
    return int(session.exec(q).one())


def claim(session: Session, task_id: int) -> bool:
    """Atomically move one specific task queued -> in_progress. False if someone else won."""
    res = session.exec(  # type: ignore[call-overload]
        update(Task)
        .where(col(Task.id) == task_id, col(Task.status) == TaskStatus.queued)
        .values(status=TaskStatus.in_progress, updated_at=utcnow())
    )
    session.commit()
    if res.rowcount == 1:
        task = session.get(Task, task_id)
        if task:
            session.refresh(task)
            log_service.log_event(
                session,
                "task_status",
                "queued -> in_progress: claimed",
                task_id=task_id,
                role="supervisor",
            )
        return True
    return False


def claim_next(session: Session) -> Task | None:
    """Claim the highest-priority queued task whose project has spare capacity."""
    settings = settings_service.get_all(session)
    if in_progress_count(session) >= settings["max_parallel_runs"]:
        return None
    per_project = settings["max_parallel_runs_per_project"]
    candidates = session.exec(
        select(Task)
        .where(Task.status == TaskStatus.queued)
        .order_by(_PRIORITY_ORDER, col(Task.created_at), col(Task.id))
    ).all()
    for task in candidates:
        if (
            task.project_id is not None
            and in_progress_count(session, task.project_id) >= per_project
        ):
            continue
        assert task.id is not None
        if claim(session, task.id):
            session.refresh(task)
            return task
    return None
