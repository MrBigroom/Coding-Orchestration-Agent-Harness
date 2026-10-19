"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, HTTPException, status
from sqlmodel import Session

from orchestrator.core.db import session_scope
from orchestrator.core.models import Project, Task
from orchestrator.core.security import require_token
from orchestrator.services import project_service, task_service


def get_db() -> Iterator[Session]:
    with session_scope() as s:
        yield s


SessionDep = Depends(get_db)
AUTH = [Depends(require_token)]


def load_task(task_id: str, session: Session = SessionDep) -> Task:
    try:
        return task_service.get(session, task_id)
    except task_service.TaskError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e


def load_project(project_id: str, session: Session = SessionDep) -> Project:
    try:
        return project_service.get(session, project_id)
    except project_service.ProjectError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
