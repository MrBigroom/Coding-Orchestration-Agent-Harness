"""Cron and one-time schedules."""

from __future__ import annotations

import json
from datetime import datetime
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
    cron_expression: str = ""
    run_at: datetime | None = None
    timezone: str = "UTC"
    agent_id: int | None = None
    model_profile: str | None = None


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
async def run_schedule(schedule_id: int, session: Session = SessionDep) -> dict[str, Any]:
    sched = scheduler_service.get(session, schedule_id)
    if sched.run_mode == "standalone":
        return dump(await scheduler_service.fire_standalone(session, sched))
    return dump(scheduler_service.fire(session, sched))


@router.delete("/schedules/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: int, session: Session = SessionDep) -> None:
    session.delete(scheduler_service.get(session, schedule_id))
    session.commit()


@router.post("/schedules/validate")
def validate(body: CronCheck, session: Session = SessionDep) -> dict[str, Any]:
    try:
        if body.run_at:
            when = scheduler_service.run_time(body.run_at, body.timezone)
            scheduler_service.check_run_time(when)
            return {"valid": True, "next_runs": [when.isoformat()], "warning": None}
        times = scheduler_service.validate_cron(body.cron_expression, timezone=body.timezone)
        warning = scheduler_service.token_plan_warning(
            session, body.cron_expression, body.agent_id, body.model_profile
        )
        return {"valid": True, "next_runs": [t.isoformat() for t in times], "warning": warning}
    except scheduler_service.CronError as e:
        return {"valid": False, "error": str(e)}
