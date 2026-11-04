"""Read-only notification history (T-9 DoD 3).

Desktop delivery is best-effort, so this list is the durable record: the GUI Notifications page
and `GET /api/notifications` read the same rows, including skipped-because-disabled and
attempted-and-failed attempts.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query
from sqlmodel import Session, col, select

from orchestrator.api.dependencies import AUTH, SessionDep
from orchestrator.core.models import Notification

router = APIRouter(dependencies=AUTH)


def dump(obj: Any) -> dict[str, Any]:
    return json.loads(obj.model_dump_json())


@router.get("/notifications")
def list_notifications(
    limit: int = Query(100, ge=1, le=2000),
    status: str | None = Query(None, pattern="^(pending|delivered|failed|skipped|coalesced)$"),
    task_id: int | None = None,
    session: Session = SessionDep,
) -> list[dict[str, Any]]:
    q = select(Notification).order_by(col(Notification.id).desc()).limit(limit)
    if status is not None:
        q = q.where(Notification.status == status)
    if task_id is not None:
        q = q.where(Notification.task_id == task_id)
    return [dump(n) for n in session.exec(q)]
