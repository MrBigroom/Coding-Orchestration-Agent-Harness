"""Cron schedules."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlmodel import Session, select

from orchestrator.api.dependencies import AUTH, SessionDep
from orchestrator.core.models import Schedule
from orchestrator.core.schemas import ScheduleCreate, ScheduleUpdate
from orchestrator.services import scheduler_service

router = APIRouter(dependencies=AUTH)


def dump(obj: Any) -> dict[str, Any]:
    return json.loads(obj.model_dump_json())


class CronCheck(BaseModel):
    cron_expression: str


@router.get("/schedules")
def list_schedules(session: Session = SessionDep) -> list[dict[str, Any]]:
    return [dump(s) for s in session.exec(select(Schedule))]


@router.post("/schedules", status_code=201)
def create_schedule(body: ScheduleCreate, session: Session = SessionDep) -> dict[str, Any]:
    return dump(scheduler_service.create(session, body))


@router.patch("/schedules/{schedule_id}")
def update_schedule(
    schedule_id: int, body: ScheduleUpdate, session: Session = SessionDep
) -> dict[str, Any]:
    return dump(
        scheduler_service.update(session, scheduler_service.get(session, schedule_id), body)
    )


@router.post("/schedules/{schedule_id}/run")
def run_schedule(schedule_id: int, session: Session = SessionDep) -> dict[str, Any]:
    task = scheduler_service.fire(session, scheduler_service.get(session, schedule_id))
    return dump(task)


@router.post("/schedules/validate")
def validate(body: CronCheck) -> dict[str, Any]:
    try:
        times = scheduler_service.validate_cron(body.cron_expression)
        return {"valid": True, "next_runs": [t.isoformat() for t in times]}
    except scheduler_service.CronError as e:
        return {"valid": False, "error": str(e)}
