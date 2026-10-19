"""Cron schedules that create (and queue) tasks from a template. Uses APScheduler 3.x."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session, select

from orchestrator.core.db import session_scope
from orchestrator.core.logging import get_logger
from orchestrator.core.models import Schedule, Task, TaskKind, TaskSource, utcnow
from orchestrator.core.schemas import ScheduleCreate, ScheduleUpdate, TaskCreate
from orchestrator.services import log_service, task_service

log = get_logger(__name__)


class CronError(ValueError):
    pass


def validate_cron(expr: str, count: int = 3) -> list[datetime]:
    """Validate a 5-field crontab expression; return the next `count` fire times (UTC)."""
    if len(expr.split()) != 5:
        raise CronError("cron expression must have exactly 5 fields (min hour dom month dow)")
    try:
        trigger = CronTrigger.from_crontab(expr, timezone=UTC)
    except (ValueError, KeyError) as e:
        raise CronError(f"invalid cron expression: {e}") from e
    times: list[datetime] = []
    prev = None
    now = datetime.now(UTC)
    for _ in range(count):
        nxt = trigger.get_next_fire_time(prev, prev or now)
        if nxt is None:
            break
        times.append(nxt)
        prev = nxt
    return times


def _template(raw: dict[str, Any], schedule_name: str) -> TaskCreate:
    data = {"title": f"[{schedule_name}] scheduled task", "kind": TaskKind.agent, **raw}
    data["source"] = TaskSource.schedule
    data["created_by"] = f"schedule:{schedule_name}"
    data.setdefault("queue", True)
    return TaskCreate.model_validate(data)


def create(session: Session, data: ScheduleCreate) -> Schedule:
    times = validate_cron(data.cron_expression)
    _template(data.task_template_json, data.name)  # validate template up front
    sched = Schedule(
        name=data.name,
        cron_expression=data.cron_expression,
        task_template_json=data.task_template_json,
        enabled=data.enabled,
        next_run_at=times[0] if times and data.enabled else None,
    )
    session.add(sched)
    session.commit()
    session.refresh(sched)
    return sched


def update(session: Session, sched: Schedule, data: ScheduleUpdate) -> Schedule:
    if data.cron_expression is not None:
        validate_cron(data.cron_expression)
        sched.cron_expression = data.cron_expression
    if data.task_template_json is not None:
        _template(data.task_template_json, sched.name)
        sched.task_template_json = data.task_template_json
    if data.enabled is not None:
        sched.enabled = data.enabled
    times = validate_cron(sched.cron_expression)
    sched.next_run_at = times[0] if sched.enabled and times else None
    sched.updated_at = utcnow()
    session.add(sched)
    session.commit()
    return sched


def get(session: Session, ref: str | int) -> Schedule:
    s = None
    if str(ref).isdigit():
        s = session.get(Schedule, int(ref))
    if s is None:
        s = session.exec(select(Schedule).where(Schedule.name == str(ref))).first()
    if not s:
        raise KeyError(f"schedule {ref!r} not found")
    return s


def fire(session: Session, sched: Schedule) -> Task:
    """Create a task from the schedule's template (and queue it unless template says not)."""
    task = task_service.create(session, _template(sched.task_template_json, sched.name))
    sched.last_run_at = utcnow()
    times = validate_cron(sched.cron_expression)
    sched.next_run_at = times[0] if sched.enabled and times else None
    session.add(sched)
    session.commit()
    log_service.log_event(
        session, "schedule_fired", f"{sched.name} -> {task.short_id}", task_id=task.id
    )
    return task


class SchedulerRunner:
    """Keeps APScheduler jobs in sync with enabled schedules (reloaded periodically)."""

    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler(timezone=UTC)
        self._signature: tuple = ()

    def start(self) -> None:
        self.scheduler.add_job(
            self.sync, "interval", seconds=30, id="__sync__", next_run_time=datetime.now(UTC)
        )
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def sync(self) -> None:
        with session_scope() as session:
            rows = list(session.exec(select(Schedule).where(Schedule.enabled == True)))  # noqa: E712
            sig = tuple((r.id, r.cron_expression, r.updated_at) for r in rows)
        if sig == self._signature:
            return
        self._signature = sig
        for job in self.scheduler.get_jobs():
            if job.id != "__sync__":
                job.remove()
        for r in rows:
            try:
                self.scheduler.add_job(
                    _fire_by_id,
                    CronTrigger.from_crontab(r.cron_expression, timezone=UTC),
                    args=[r.id],
                    id=f"schedule-{r.id}",
                    misfire_grace_time=300,
                    coalesce=True,
                    max_instances=1,
                )
            except ValueError as e:
                log.warning("skipping schedule %s: %s", r.name, e)


def _fire_by_id(schedule_id: int) -> None:
    with session_scope() as session:
        sched = session.get(Schedule, schedule_id)
        if sched and sched.enabled:
            fire(session, sched)
