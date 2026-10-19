"""Tasks, runs, reviews, verifications."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, col, select

from orchestrator.api.dependencies import AUTH, SessionDep, load_task
from orchestrator.core.models import (
    MemoryEntry,
    Project,
    Review,
    Run,
    Task,
    TaskStatus,
    Verification,
)
from orchestrator.core.schemas import DecisionRequest, RetryRequest, TaskCreate, TaskUpdate
from orchestrator.services import (
    artifact_service,
    planning_service,
    queue_service,
    run_service,
    supervisor_service,
    task_service,
)

router = APIRouter(dependencies=AUTH)


def dump(obj: Any) -> dict[str, Any]:
    return json.loads(obj.model_dump_json())


def _detail(session: Session, task: Task) -> dict[str, Any]:
    data = dump(task)
    data["runs"] = [dump(r) for r in task_service.runs_for(session, task)]
    data["reviews"] = [
        dump(r)
        for r in session.exec(
            select(Review).where(Review.task_id == task.id).order_by(col(Review.id).desc())
        )
    ]
    data["verifications"] = [
        dump(v)
        for v in session.exec(
            select(Verification)
            .where(Verification.task_id == task.id)
            .order_by(col(Verification.id).desc())
        )
    ]
    run_tags = {f"R-{r['id']}" for r in data["runs"]}
    data["memory_suggestions"] = [
        dump(m) for m in session.exec(select(MemoryEntry)) if m.source.split(":")[-1] in run_tags
    ]
    if (
        task.status == TaskStatus.accepted
        and task.merge_status in ("ready", "merge_refused", "stale")
        and task.project_id
    ):
        from pathlib import Path

        from orchestrator.services import git_service

        proj = session.get(Project, task.project_id)
        if proj and task.branch_name:
            data["merge_command"] = git_service.merge_command(
                Path(proj.path), proj.base_branch, task.branch_name
            )
    return data


@router.get("/tasks")
def list_tasks(
    project_id: int | None = None,
    status: str | None = None,
    kind: str | None = None,
    risk: str | None = None,
    tag: str | None = None,
    include_archived: bool = False,
    session: Session = SessionDep,
) -> list[dict[str, Any]]:
    return [
        dump(t)
        for t in task_service.list_tasks(
            session,
            project_id=project_id,
            status=status,
            kind=kind,
            risk=risk,
            tag=tag,
            include_archived=include_archived,
        )
    ]


@router.post("/tasks", status_code=201)
def create_task(body: TaskCreate, session: Session = SessionDep) -> dict[str, Any]:
    if body.project_id is not None and not session.get(Project, body.project_id):
        raise HTTPException(404, f"project {body.project_id} not found")
    return dump(task_service.create(session, body))


@router.get("/tasks/{task_id}")
def get_task(task: Task = Depends(load_task), session: Session = SessionDep) -> dict[str, Any]:
    return _detail(session, task)


@router.patch("/tasks/{task_id}")
def update_task(
    body: TaskUpdate, task: Task = Depends(load_task), session: Session = SessionDep
) -> dict[str, Any]:
    return dump(task_service.update(session, task, body))


@router.post("/tasks/{task_id}/queue")
def queue_task(task: Task = Depends(load_task), session: Session = SessionDep) -> dict[str, Any]:
    return dump(task_service.queue(session, task))


@router.post("/tasks/{task_id}/run", status_code=202)
async def run_task(
    body: RetryRequest | None = None, task: Task = Depends(load_task), session: Session = SessionDep
) -> dict[str, Any]:
    """Queue (if needed), claim, and start the run in the background."""
    from orchestrator.api.main import background

    if task.status in (
        TaskStatus.draft,
        TaskStatus.planned,
        TaskStatus.rejected,
        TaskStatus.blocked,
    ):
        task_service.queue(session, task)
    if task.status != TaskStatus.queued or not queue_service.claim(session, task.id or 0):
        raise HTTPException(409, f"task {task.short_id} cannot be started ({task.status})")
    harness = body.harness_adapter if body else None
    profile = body.provider_profile if body else None

    async def _go(task_id: int) -> None:
        from orchestrator.core.db import session_scope

        with session_scope() as s:
            await run_service.execute(s, task_service.get(s, task_id), harness, profile)

    background.spawn(_go(task.id or 0))
    return {"started": task.short_id}


@router.get("/tasks/{task_id}/dry-run")
def dry_run(task: Task = Depends(load_task), session: Session = SessionDep) -> dict[str, Any]:
    return run_service.dry_run(session, task).as_dict()


@router.post("/tasks/{task_id}/cancel")
def cancel(task: Task = Depends(load_task), session: Session = SessionDep) -> dict[str, Any]:
    return run_service.cancel(session, task)


@router.post("/tasks/{task_id}/retry")
def retry(
    body: RetryRequest | None = None, task: Task = Depends(load_task), session: Session = SessionDep
) -> dict[str, Any]:
    body = body or RetryRequest()
    return dump(run_service.retry(session, task, body.harness_adapter, body.provider_profile))


@router.post("/tasks/{task_id}/block")
def block(
    body: DecisionRequest, task: Task = Depends(load_task), session: Session = SessionDep
) -> dict[str, Any]:
    if not body.reason.strip():
        raise HTTPException(400, "a reason is required to block a task")
    return dump(task_service.block(session, task, body.reason))


@router.post("/tasks/{task_id}/unblock")
def unblock(
    body: DecisionRequest | None = None,
    task: Task = Depends(load_task),
    session: Session = SessionDep,
) -> dict[str, Any]:
    return dump(task_service.unblock(session, task, body.reason if body else ""))


@router.post("/tasks/{task_id}/archive")
def archive(task: Task = Depends(load_task), session: Session = SessionDep) -> dict[str, Any]:
    return dump(task_service.archive(session, task))


@router.post("/tasks/{task_id}/accept")
def accept(
    body: DecisionRequest | None = None,
    task: Task = Depends(load_task),
    session: Session = SessionDep,
) -> dict[str, Any]:
    return run_service.human_decision(session, task, True, body.reason if body else "")


@router.post("/tasks/{task_id}/reject")
def reject(
    body: DecisionRequest | None = None,
    task: Task = Depends(load_task),
    session: Session = SessionDep,
) -> dict[str, Any]:
    return run_service.human_decision(session, task, False, body.reason if body else "")


@router.post("/tasks/{task_id}/escalate")
def escalate(task: Task = Depends(load_task), session: Session = SessionDep) -> dict[str, Any]:
    return dump(run_service.escalate(session, task))


@router.post("/tasks/{task_id}/merge")
def merge(task: Task = Depends(load_task), session: Session = SessionDep) -> dict[str, Any]:
    proj = session.get(Project, task.project_id) if task.project_id else None
    if not proj:
        raise HTTPException(400, "task has no project")
    return {"merge_sha": supervisor_service.manual_merge(session, task, proj)}


@router.post("/tasks/{task_id}/plan")
async def plan(
    task: Task = Depends(load_task), session: Session = SessionDep
) -> list[dict[str, Any]]:
    proj = session.get(Project, task.project_id) if task.project_id else None
    return [dump(t) for t in await planning_service.plan_phases(session, task, proj)]


# ---------------------------------------------------------------- runs/reviews/verifications


@router.get("/runs")
def list_runs(
    task_id: int | None = None,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    session: Session = SessionDep,
) -> list[dict[str, Any]]:
    q = select(Run).order_by(col(Run.id).desc()).limit(limit)
    if task_id is not None:
        q = q.where(Run.task_id == task_id)
    if status:
        q = q.where(Run.status == status)
    return [dump(r) for r in session.exec(q)]


@router.get("/runs/{run_id}")
def get_run(run_id: int, session: Session = SessionDep) -> dict[str, Any]:
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    data = dump(run)
    data["artifacts"] = artifact_service.list_artifacts(run_id)
    return data


@router.get("/reviews")
def list_reviews(task_id: int | None = None, session: Session = SessionDep) -> list[dict[str, Any]]:
    q = select(Review).order_by(col(Review.id).desc())
    if task_id is not None:
        q = q.where(Review.task_id == task_id)
    return [dump(r) for r in session.exec(q)]


@router.get("/verifications")
def list_verifications(
    task_id: int | None = None, session: Session = SessionDep
) -> list[dict[str, Any]]:
    q = select(Verification).order_by(col(Verification.id).desc())
    if task_id is not None:
        q = q.where(Verification.task_id == task_id)
    return [dump(v) for v in session.exec(q)]
