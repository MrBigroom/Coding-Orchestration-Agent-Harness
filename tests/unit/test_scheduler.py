import pytest

from orchestrator.core.schemas import ScheduleCreate
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
