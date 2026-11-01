"""Cron and one-time schedules that create (and queue) tasks from a template. Uses
APScheduler 3.x."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlmodel import Session, select

from orchestrator.core.db import session_scope
from orchestrator.core.logging import get_logger
from orchestrator.core.models import Schedule, Task, TaskKind, TaskRisk, TaskSource, utcnow
from orchestrator.core.schemas import AgentRun, ScheduleCreate, ScheduleUpdate, TaskCreate
from orchestrator.services import agent_service, log_service, task_service

log = get_logger(__name__)


class CronError(ValueError):
    pass


DAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
LATE_SECONDS = 300


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as e:
        raise CronError(f"unknown time zone: {name!r}") from e


def _day_names(field: str) -> str:
    """Spell out numeric weekdays. Crontab counts from Sunday (0 or 7), but APScheduler reads
    the same digits counting from Monday, which would run every schedule a day late."""
    names: list[str] = []
    for item in field.split(","):
        m = re.fullmatch(r"(\*|\d)(?:-(\d))?(?:/(\d+))?", item)
        if not m:
            names.append(item)
            continue
        first, last, step = m.groups()
        start, end = (0, 6) if first == "*" else (int(first), int(last or first))
        if end > 7 or start > end or step == "0":
            raise CronError(f"invalid day of week: {item!r}")
        if step and not last and first != "*":
            end = 6
        names += [DAY_NAMES[d % 7] for d in range(start, end + 1, int(step or 1))]
    return ",".join(dict.fromkeys(names))


def _cron_trigger(expr: str, timezone: str = "UTC") -> CronTrigger:
    fields = expr.split()
    if len(fields) != 5:
        raise CronError("cron expression must have exactly 5 fields (min hour dom month dow)")
    zone = _zone(timezone)
    if fields[4] != "*":
        fields[4] = _day_names(fields[4])
    try:
        return CronTrigger.from_crontab(" ".join(fields), timezone=zone)
    except (ValueError, KeyError) as e:
        raise CronError(f"invalid cron expression: {e}") from e


def validate_cron(expr: str, count: int = 3, timezone: str = "UTC") -> list[datetime]:
    """Validate a 5-field crontab expression; return the next `count` fire times (UTC)."""
    trigger = _cron_trigger(expr, timezone)
    times: list[datetime] = []
    prev = None
    now = datetime.now(UTC)
    for _ in range(count):
        nxt = trigger.get_next_fire_time(prev, prev or now)
        if nxt is None:
            break
        times.append(nxt.astimezone(UTC))
        prev = nxt
    return times


def run_time(value: datetime, timezone: str = "UTC") -> datetime:
    """The UTC moment of a one-time schedule. A value with no offset is read in `timezone`."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=_zone(timezone))
    return value.astimezone(UTC)


def check_run_time(when: datetime) -> None:
    if when <= datetime.now(UTC):
        raise CronError("that time has already passed; choose a later one")


def _next_run(sched: Schedule) -> datetime | None:
    if not sched.enabled:
        return None
    if sched.run_at:
        return sched.run_at
    times = validate_cron(sched.cron_expression, timezone=sched.timezone)
    return times[0] if times else None


def _template(raw: dict[str, Any], schedule_name: str) -> TaskCreate:
    data = {"title": f"[{schedule_name}] scheduled task", "kind": TaskKind.agent, **raw}
    data["source"] = TaskSource.schedule
    data["created_by"] = f"schedule:{schedule_name}"
    data.setdefault("queue", True)
    return TaskCreate.model_validate(data)


def _agent_run(raw: dict[str, Any], schedule_name: str) -> AgentRun:
    """For agent schedules the template only carries the request (and optional title, risk
    and DoD); model, harness and project come from the agent."""
    request = str(raw.get("description") or raw.get("title") or "").strip()
    if not request:
        raise ValueError(f"schedule {schedule_name!r}: agent schedules need a description")
    return AgentRun(
        request=request,
        title=raw.get("title") or None,
        risk=raw.get("risk", TaskRisk.low),
        definition_of_done=raw.get("definition_of_done", []),
        queue=raw.get("queue", True),
    )


def _validate(session: Session, raw: dict[str, Any], name: str, agent_id: int | None) -> None:
    if agent_id:
        agent_service.get(session, agent_id)
        _agent_run(raw, name)
    else:
        _template(raw, name)


def _check_name(session: Session, name: str, own_id: int | None = None) -> None:
    if not name.strip():
        raise ValueError("a schedule needs a name")
    other = session.exec(select(Schedule).where(Schedule.name == name)).first()
    if other and other.id != own_id:
        raise ValueError(f"a schedule named {name!r} already exists")


def create(session: Session, data: ScheduleCreate) -> Schedule:
    _check_name(session, data.name)
    if bool(data.cron_expression) == bool(data.run_at):
        raise CronError("set either a cron expression or a run time")
    _zone(data.timezone)
    run_at = run_time(data.run_at, data.timezone) if data.run_at else None
    if run_at:
        check_run_time(run_at)
    _validate(session, data.task_template_json, data.name, data.agent_id)
    sched = Schedule(
        name=data.name,
        cron_expression=data.cron_expression,
        run_at=run_at,
        timezone=data.timezone,
        task_template_json=data.task_template_json,
        agent_id=data.agent_id,
        enabled=data.enabled,
    )
    sched.next_run_at = _next_run(sched)
    session.add(sched)
    session.commit()
    session.refresh(sched)
    return sched


def update(session: Session, sched: Schedule, data: ScheduleUpdate) -> Schedule:
    if data.cron_expression is not None and data.run_at:
        raise CronError("set either a cron expression or a run time")
    if data.name is not None:
        _check_name(session, data.name, sched.id)
    zone = data.timezone or sched.timezone
    _zone(zone)
    if data.cron_expression is not None:
        validate_cron(data.cron_expression, timezone=zone)
        sched.cron_expression = data.cron_expression
        sched.run_at = None
    if data.run_at:
        sched.run_at = run_time(data.run_at, zone)
        sched.cron_expression = ""
        check_run_time(sched.run_at)
    elif sched.run_at and data.enabled and not sched.enabled:
        check_run_time(sched.run_at)
    sched.timezone = zone
    if data.name is not None:
        sched.name = data.name
    if data.agent_id is not None:
        sched.agent_id = data.agent_id or None
    if data.task_template_json is not None:
        sched.task_template_json = data.task_template_json
    if data.agent_id is not None or data.task_template_json is not None:
        _validate(session, sched.task_template_json, sched.name, sched.agent_id)
    if data.enabled is not None:
        sched.enabled = data.enabled
    sched.next_run_at = _next_run(sched)
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
    """Create a task from the schedule's template (and queue it unless template says not).
    Agent schedules run the agent, so the task uses the agent's model, harness and project.
    A one-time schedule turns itself off once its time has come; running it by hand before
    then leaves it pending."""
    if sched.agent_id:
        task = agent_service.run(
            session,
            agent_service.get(session, sched.agent_id),
            _agent_run(sched.task_template_json, sched.name),
            source=TaskSource.schedule,
            created_by=f"schedule:{sched.name}",
        )
    else:
        task = task_service.create(session, _template(sched.task_template_json, sched.name))
    now = utcnow()
    sched.last_run_at = now
    note = ""
    if sched.run_at and now >= sched.run_at:
        sched.enabled = False
        sched.updated_at = now
        if (now - sched.run_at).total_seconds() > LATE_SECONDS:
            note = f" (late: was due {sched.run_at:%Y-%m-%d %H:%M} UTC)"
    sched.next_run_at = _next_run(sched)
    session.add(sched)
    session.commit()
    log_service.log_event(
        session, "schedule_fired", f"{sched.name} -> {task.short_id}{note}", task_id=task.id
    )
    return task


def token_plan_warning(
    session: Session, expr: str, agent_id: int | None = None, profile: str | None = None
) -> str | None:
    """Token Plan terms rule out high-frequency automation (docs/providers.md). Warn when a
    schedule on a Token Plan model fires more often than once a day."""
    try:
        times = validate_cron(expr, count=4)
    except CronError:
        return None
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)]
    if not gaps or min(gaps) >= 86400:
        return None
    if agent_id:
        try:
            agent = agent_service.get(session, agent_id)
        except KeyError:
            return None
        profile = agent_service.routing(session, agent).get("profile")
    elif not profile:
        profile = "agent-flash"  # default for scheduled agent-kind tasks
    if not agent_service.uses_token_plan(session, profile):
        return None
    return (
        f"Runs more than once a day on a Token Plan model ({profile}). The Token Plan "
        "terms exclude high-frequency automated use; prefer daily or less, or a "
        "pay-as-you-go/local model."
    )


class SchedulerRunner:
    """Keeps APScheduler jobs in sync with enabled schedules (reloaded periodically).
    A one-time schedule whose time passed while the server was off runs on the next sync."""

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
            sig = tuple((r.id, r.cron_expression, r.run_at, r.updated_at) for r in rows)
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
                    _job_trigger(r),
                    args=[r.id],
                    id=f"schedule-{r.id}",
                    misfire_grace_time=None if r.run_at else 300,
                    coalesce=True,
                    max_instances=1,
                )
            except ValueError as e:
                log.warning("skipping schedule %s: %s", r.name, e)


def _job_trigger(sched: Schedule) -> CronTrigger | DateTrigger:
    if sched.run_at:
        return DateTrigger(max(sched.run_at, datetime.now(UTC)), timezone=UTC)
    return _cron_trigger(sched.cron_expression, sched.timezone)


def _fire_by_id(schedule_id: int) -> None:
    with session_scope() as session:
        sched = session.get(Schedule, schedule_id)
        if sched and sched.enabled:
            fire(session, sched)
