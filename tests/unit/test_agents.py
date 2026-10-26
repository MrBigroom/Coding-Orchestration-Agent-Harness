import pytest

from orchestrator.core.schemas import (
    AgentCreate,
    AgentRun,
    AgentUpdate,
    ProjectCreate,
    ScheduleCreate,
)
from orchestrator.services import agent_service, project_service, scheduler_service
from tests.conftest import make_repo, requires_git


@pytest.fixture
def project(session, tmp_path):
    repo = make_repo(tmp_path / "repo")
    return project_service.create(session, ProjectCreate(name="app", path=str(repo)))


@requires_git
def test_agent_with_project_runs_as_gated_coding_task(session, project):
    a = agent_service.create(
        session,
        AgentCreate(
            name="refactorer",
            instructions="Keep diffs small.",
            model_profile="worker-glm",
            project_id=project.id,
            tags=["Backend"],
        ),
    )
    t = agent_service.run(session, a, AgentRun(request="Split utils.py\nmore detail"))
    assert t.kind == "coding" and t.status == "queued" and t.branch_name
    assert t.agent_id == a.id and t.project_id == project.id
    assert t.assigned_provider_profile == "worker-glm" and t.assigned_harness_adapter is None
    assert t.title == "[refactorer] Split utils.py"
    assert t.description.index("Keep diffs small.") < t.description.index("Split utils.py")
    assert set(t.tags) == {"backend", "agent:refactorer"}
    r = agent_service.routing(session, a)
    assert r["profile"] == "worker-glm" and r["harness_reason"]


def test_agent_without_project_is_direct_agent_task(session):
    a = agent_service.create(session, AgentCreate(name="writer", tags=["mock"]))
    t = agent_service.run(session, a, AgentRun(request="Summarise", queue=False))
    assert t.kind == "agent" and t.status == "planned" and t.branch_name is None
    r = agent_service.routing(session, a)
    assert r["profile"] == "mock-agent" and r["harness"] is None


def test_agent_validation(session):
    with pytest.raises(KeyError):
        agent_service.create(session, AgentCreate(name="x", model_profile="nope"))
    with pytest.raises(KeyError):
        agent_service.create(session, AgentCreate(name="x", harness_adapter="nope"))
    agent_service.create(session, AgentCreate(name="x"))
    with pytest.raises(agent_service.AgentError):
        agent_service.create(session, AgentCreate(name="x"))
    a = agent_service.update(
        session, agent_service.get(session, "x"), AgentUpdate(model_profile="worker-glm")
    )
    assert a.model_profile == "worker-glm"
    a = agent_service.update(session, a, AgentUpdate(model_profile="", enabled=False))
    assert a.model_profile is None
    with pytest.raises(agent_service.AgentError):
        agent_service.run(session, a, AgentRun(request="r"))


def test_schedule_runs_agent(session):
    a = agent_service.create(
        session, AgentCreate(name="auditor", model_profile="mock-agent", instructions="Be brief.")
    )
    with pytest.raises(ValueError):
        scheduler_service.create(
            session, ScheduleCreate(name="s", cron_expression="0 2 * * *", agent_id=a.id)
        )
    sched = scheduler_service.create(
        session,
        ScheduleCreate(
            name="s",
            cron_expression="0 2 * * *",
            agent_id=a.id,
            task_template_json={"description": "Audit deps"},
        ),
    )
    t = scheduler_service.fire(session, sched)
    assert t.agent_id == a.id and t.source == "schedule" and t.created_by == "schedule:s"
    assert t.assigned_provider_profile == "mock-agent" and "Be brief." in t.description
    with pytest.raises(agent_service.AgentError):
        agent_service.delete(session, a)


def test_token_plan_warning(session):
    a = agent_service.create(session, AgentCreate(name="tp"))  # default agent-flash: Token Plan
    m = agent_service.create(session, AgentCreate(name="m", model_profile="mock-agent"))
    assert scheduler_service.token_plan_warning(session, "*/5 * * * *", a.id)
    assert scheduler_service.token_plan_warning(session, "0 2 * * *", a.id) is None
    assert scheduler_service.token_plan_warning(session, "*/5 * * * *", m.id) is None
    assert scheduler_service.token_plan_warning(session, "0 * * * *")  # template default
