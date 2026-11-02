from datetime import UTC, datetime, timedelta

import pytest

from orchestrator.core.schemas import ScheduleCreate, ScheduleUpdate
from orchestrator.services import scheduler_service


@pytest.mark.parametrize("expr", ["*/5 * * * *", "0 2 * * *", "30 9 * * 1-5", "0 0 1 * *"])
def test_valid_cron(expr):
    times = scheduler_service.validate_cron(expr)
    assert len(times) == 3 and times[0] < times[1] < times[2]


@pytest.mark.parametrize("expr", ["", "* * * *", "61 * * * *", "* * * * * *", "bad cron x y z"])
def test_invalid_cron(expr):
    with pytest.raises(scheduler_service.CronError):
        scheduler_service.validate_cron(expr)


def test_schedule_fire_creates_queued_agent_task(session):
    sched = scheduler_service.create(
        session,
        ScheduleCreate(
            name="nightly",
            cron_expression="0 2 * * *",
            task_template_json={"title": "audit", "tags": ["mock"]},
        ),
    )
    assert sched.next_run_at is not None
    task = scheduler_service.fire(session, sched)
    assert task.status == "queued" and task.kind == "agent" and task.source == "schedule"
    assert sched.last_run_at is not None


def test_schedule_rejects_bad_template(session):
    with pytest.raises(ValueError):
        scheduler_service.create(
            session,
            ScheduleCreate(
                name="bad", cron_expression="0 2 * * *", task_template_json={"kind": "nope"}
            ),
        )


def test_scheduler_runner_registers_enabled_schedules(session):
    from orchestrator.core.schemas import ScheduleUpdate
    from orchestrator.services.scheduler_service import SchedulerRunner

    a = scheduler_service.create(session, ScheduleCreate(name="a", cron_expression="*/5 * * * *"))
    b = scheduler_service.create(session, ScheduleCreate(name="b", cron_expression="0 3 * * *"))
    scheduler_service.update(session, b, ScheduleUpdate(enabled=False))
    runner = SchedulerRunner()
    runner.sync()
    ids = {j.id for j in runner.scheduler.get_jobs()}
    assert f"schedule-{a.id}" in ids and f"schedule-{b.id}" not in ids


def test_fire_by_id_creates_task(session):
    from sqlmodel import select

    from orchestrator.core.models import Task
    from orchestrator.services.scheduler_service import _fire_by_id

    s = scheduler_service.create(
        session,
        ScheduleCreate(name="c", cron_expression="* * * * *", task_template_json={"title": "tick"}),
    )
    _fire_by_id(s.id)
    titles = [t.title for t in session.exec(select(Task))]
    assert "tick" in titles


def later(**kwargs) -> datetime:
    return datetime.now(UTC) + timedelta(**(kwargs or {"days": 1}))


@pytest.mark.parametrize(
    ("day", "expected"),
    [("0", {"Sunday"}), ("7", {"Sunday"}), ("1", {"Monday"}), ("mon", {"Monday"})],
)
def test_cron_weekdays_count_from_sunday(day, expected):
    times = scheduler_service.validate_cron(f"0 12 * * {day}")
    assert {t.strftime("%A") for t in times} == expected


def test_cron_weekday_ranges():
    days = {t.strftime("%a") for t in scheduler_service.validate_cron("0 12 * * 1-5", count=7)}
    assert days == {"Mon", "Tue", "Wed", "Thu", "Fri"}
    days = {t.strftime("%a") for t in scheduler_service.validate_cron("0 12 * * 0-2", count=7)}
    assert days == {"Sun", "Mon", "Tue"}
    days = {t.strftime("%a") for t in scheduler_service.validate_cron("0 12 * * */2", count=7)}
    assert days == {"Sun", "Tue", "Thu", "Sat"}


@pytest.mark.parametrize("expr", ["0 0 * * 8", "0 0 * * 5-2", "0 0 * * 1/0"])
def test_invalid_cron_weekdays(expr):
    with pytest.raises(scheduler_service.CronError):
        scheduler_service.validate_cron(expr)


def test_cron_hours_follow_the_time_zone():
    times = scheduler_service.validate_cron("0 9 * * *", timezone="Asia/Kuala_Lumpur")
    assert all(t.hour == 1 and t.tzinfo == UTC for t in times)


def test_cron_keeps_local_time_across_daylight_saving():
    start = datetime(2027, 3, 12, 20, tzinfo=UTC)
    trigger = scheduler_service._cron_trigger("0 9 * * *", "America/New_York")
    first = trigger.get_next_fire_time(None, start)
    second = trigger.get_next_fire_time(first, first)
    assert first.astimezone(UTC).hour == 14 and second.astimezone(UTC).hour == 13


def test_unknown_time_zone(session):
    with pytest.raises(scheduler_service.CronError):
        scheduler_service.create(
            session, ScheduleCreate(name="x", cron_expression="0 2 * * *", timezone="Mars/Base")
        )


def test_run_time_reads_plain_values_in_the_time_zone():
    plain = datetime(2030, 1, 2, 9, 0)
    assert scheduler_service.run_time(plain, "Asia/Kuala_Lumpur") == datetime(
        2030, 1, 2, 1, 0, tzinfo=UTC
    )
    offset = datetime(2030, 1, 2, 9, 0, tzinfo=UTC)
    assert scheduler_service.run_time(offset, "Asia/Kuala_Lumpur") == offset


def test_schedule_needs_exactly_one_timing(session):
    with pytest.raises(ValueError):
        scheduler_service.create(session, ScheduleCreate(name="none"))
    with pytest.raises(ValueError):
        scheduler_service.create(
            session, ScheduleCreate(name="both", cron_expression="0 2 * * *", run_at=later())
        )


def test_one_time_schedule_refuses_a_past_time(session):
    with pytest.raises(scheduler_service.CronError):
        scheduler_service.create(session, ScheduleCreate(name="p", run_at=later(minutes=-1)))


def test_one_time_schedule_turns_off_after_its_time(session):
    s = scheduler_service.create(
        session, ScheduleCreate(name="once", run_at=later(), task_template_json={"title": "t"})
    )
    assert s.cron_expression == "" and s.next_run_at is not None
    s.run_at = later(minutes=-1)
    task = scheduler_service.fire(session, s)
    assert task.source == "schedule"
    assert s.enabled is False and s.next_run_at is None and s.last_run_at is not None


def test_running_a_one_time_schedule_early_leaves_it_pending(session):
    s = scheduler_service.create(
        session, ScheduleCreate(name="early", run_at=later(), task_template_json={"title": "t"})
    )
    scheduler_service.fire(session, s)
    assert s.enabled is True and s.next_run_at is not None


def test_finished_one_time_schedule_needs_a_new_time(session):
    s = scheduler_service.create(
        session, ScheduleCreate(name="again", run_at=later(), task_template_json={"title": "t"})
    )
    s.run_at = later(minutes=-1)
    scheduler_service.fire(session, s)
    with pytest.raises(scheduler_service.CronError):
        scheduler_service.update(session, s, ScheduleUpdate(enabled=True))
    session.refresh(s)
    new_time = later(days=2)
    scheduler_service.update(session, s, ScheduleUpdate(enabled=True, run_at=new_time))
    assert s.enabled is True


def test_a_schedule_can_switch_between_once_and_cron(session):
    s = scheduler_service.create(session, ScheduleCreate(name="sw", cron_expression="0 2 * * *"))
    scheduler_service.update(session, s, ScheduleUpdate(run_at=later()))
    assert s.cron_expression == "" and s.run_at is not None
    scheduler_service.update(session, s, ScheduleUpdate(cron_expression="0 3 * * *"))
    assert s.run_at is None and s.cron_expression == "0 3 * * *"


def test_runner_registers_a_future_one_time_schedule(session):
    from orchestrator.services.scheduler_service import SchedulerRunner

    when = later()
    s = scheduler_service.create(session, ScheduleCreate(name="f", run_at=when))
    runner = SchedulerRunner()
    runner.sync()
    job = next(j for j in runner.scheduler.get_jobs() if j.id == f"schedule-{s.id}")
    assert abs(job.trigger.run_date - when) < timedelta(seconds=1)


def test_missed_one_time_schedule_runs_on_the_next_sync(session):
    from orchestrator.services.scheduler_service import SchedulerRunner, _job_trigger

    s = scheduler_service.create(
        session, ScheduleCreate(name="m", run_at=later(), task_template_json={"title": "t"})
    )
    s.run_at = datetime(2020, 1, 1, tzinfo=UTC)
    session.add(s)
    session.commit()
    assert _job_trigger(s).run_date > datetime(2021, 1, 1, tzinfo=UTC)
    runner = SchedulerRunner()
    runner.sync()
    job = next(j for j in runner.scheduler.get_jobs() if j.id == f"schedule-{s.id}")
    assert job.misfire_grace_time is None


def test_late_run_is_noted_in_the_log(session):
    from sqlmodel import select

    from orchestrator.core.models import BuildLogEntry

    s = scheduler_service.create(
        session, ScheduleCreate(name="late", run_at=later(), task_template_json={"title": "t"})
    )
    s.run_at = later(hours=-2)
    scheduler_service.fire(session, s)
    notes = [e.details for e in session.exec(select(BuildLogEntry))]
    assert any("late" in n and "was due" in n for n in notes)


def test_renaming_a_schedule(session):
    a = scheduler_service.create(session, ScheduleCreate(name="a", cron_expression="0 2 * * *"))
    scheduler_service.create(session, ScheduleCreate(name="b", cron_expression="0 2 * * *"))
    scheduler_service.update(session, a, ScheduleUpdate(name="a"))
    scheduler_service.update(session, a, ScheduleUpdate(name="renamed"))
    assert a.name == "renamed"
    for name in ("b", " "):
        with pytest.raises(ValueError):
            scheduler_service.update(session, a, ScheduleUpdate(name=name))
    assert a.name == "renamed"


def test_names_stay_unique(session):
    scheduler_service.create(session, ScheduleCreate(name="a", cron_expression="0 2 * * *"))
    with pytest.raises(ValueError):
        scheduler_service.create(session, ScheduleCreate(name="a", cron_expression="0 3 * * *"))


def test_changing_the_template_keeps_a_finished_schedule_off(session):
    s = scheduler_service.create(
        session, ScheduleCreate(name="kept", run_at=later(), task_template_json={"title": "t"})
    )
    s.run_at = later(minutes=-1)
    scheduler_service.fire(session, s)
    scheduler_service.update(session, s, ScheduleUpdate(task_template_json={"title": "new"}))
    assert s.enabled is False and s.next_run_at is None
    assert s.task_template_json == {"title": "new"}


def test_changing_the_time_moves_the_next_run(session):
    s = scheduler_service.create(session, ScheduleCreate(name="m", cron_expression="0 2 * * *"))
    scheduler_service.update(
        session, s, ScheduleUpdate(cron_expression="0 9 * * *", timezone="Asia/Kuala_Lumpur")
    )
    assert s.next_run_at.hour == 1 and s.timezone == "Asia/Kuala_Lumpur"
    when = later(days=3)
    scheduler_service.update(session, s, ScheduleUpdate(run_at=when))
    assert s.next_run_at == when
