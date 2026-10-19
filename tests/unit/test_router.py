from orchestrator.core.schemas import TaskCreate
from orchestrator.services import adapter_service, provider_service, task_service


def _task(session, **kw):
    return task_service.create(session, TaskCreate(title="t", **kw))


def test_default_profiles_by_role(session):
    t = _task(session)
    assert (
        provider_service.choose_profile(session, t, "architect").profile.model_name
        == "claude-opus-5-5"
    )
    assert (
        provider_service.choose_profile(session, t, "supervisor").profile.model_name
        == "qwen3.8-max"
    )
    assert (
        provider_service.choose_profile(session, t, "worker").profile.model_name == "qwen3.8-flash"
    )


def test_tag_routing_for_worker_profiles(session):
    assert (
        provider_service.choose_profile(
            session, _task(session, tags=["deepseek"]), "worker"
        ).profile.model_name
        == "deepseek-v4.1-flash"
    )
    assert (
        provider_service.choose_profile(
            session, _task(session, tags=["glm"]), "worker"
        ).profile.model_name
        == "glm-5.3"
    )


def test_agent_tasks_prefer_flash_and_avoid_opus(session):
    t = _task(session, kind="agent")
    choice = provider_service.choose_profile(session, t, "agent")
    assert choice.profile.name == "agent-flash"
    escalated = _task(session, kind="agent", tags=["escalate"])
    assert (
        provider_service.choose_profile(session, escalated, "agent").profile.name
        == "architect-claude-cli"
    )


def test_hermes_never_default(session):
    t = _task(session, kind="agent")
    assert provider_service.choose_profile(session, t, "agent").profile.name != "hermes-local"


def test_mock_tag_routes_to_mock_everything(session):
    t = _task(session, tags=["mock"])
    assert provider_service.choose_profile(session, t, "architect").profile.name == "mock-architect"
    assert adapter_service.choose_harness(session, t).adapter == "mock"


def test_harness_fallbacks_when_no_cli_installed(session, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    low = _task(session, tags=["deepseek"])
    assert adapter_service.choose_harness(session, low).adapter is None  # direct API worker
    high = _task(session, risk="high")
    choice = adapter_service.choose_harness(session, high)
    assert choice.adapter is None and "block" in choice.reason


def test_harness_prefers_tagged_cli_when_available(session, monkeypatch):
    monkeypatch.setattr(
        adapter_service, "available", lambda s, name: name in {"deepseek_harness", "opencode"}
    )
    assert (
        adapter_service.choose_harness(session, _task(session, tags=["deepseek"])).adapter
        == "deepseek_harness"
    )
    assert (
        adapter_service.choose_harness(session, _task(session, tags=["qwen"])).adapter == "opencode"
    )


def test_explicit_assignment_wins(session):
    t = _task(
        session,
        tags=["deepseek"],
        assigned_harness_adapter="aider",
        assigned_provider_profile="worker-glm",
    )
    assert adapter_service.choose_harness(session, t).adapter == "aider"
    assert provider_service.choose_profile(session, t, "worker").profile.name == "worker-glm"
