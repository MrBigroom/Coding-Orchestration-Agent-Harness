import pytest

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


def test_qwen_tag_prefers_qwen_code_when_available(session, monkeypatch):
    monkeypatch.setattr(
        adapter_service, "available", lambda s, name: name in {"pi", "qwen_code", "opencode"}
    )
    task = _task(session, tags=["qwen"])
    assert adapter_service.choose_harness(session, task).adapter == "qwen_code"


def test_qwen_tag_falls_back_to_route_without_qwen_code(session, monkeypatch):
    monkeypatch.setattr(adapter_service, "available", lambda s, name: name == "opencode")
    task = _task(session, tags=["qwen"])
    assert adapter_service.choose_harness(session, task).adapter == "opencode"


@pytest.mark.parametrize(
    ("tag", "second_choice"), [("deepseek", "deepseek_harness"), ("glm", "zcode")]
)
def test_model_tags_follow_the_route(session, monkeypatch, tag, second_choice):
    installed = {"pi", second_choice, "opencode"}
    monkeypatch.setattr(adapter_service, "available", lambda s, name: name in installed)
    task = _task(session, tags=[tag])
    assert adapter_service.choose_harness(session, task).adapter == "pi"
    installed.discard("pi")
    assert adapter_service.choose_harness(session, task).adapter == second_choice


def test_harness_follows_worker_model(session, monkeypatch):
    monkeypatch.setattr(
        adapter_service, "available", lambda s, name: name in {"pi", "deepseek_harness", "opencode"}
    )
    default = adapter_service.choose_harness(session, _task(session))
    assert default.adapter == "pi" and "qwen3.8-flash" in default.reason
    ds = _task(session, assigned_provider_profile="worker-deepseek-flash")
    assert adapter_service.choose_harness(session, ds).adapter == "pi"
    glm = _task(session, assigned_provider_profile="worker-glm")
    assert adapter_service.choose_harness(session, glm).adapter == "pi"


def test_kimi_models_prefer_pi(session, monkeypatch):
    monkeypatch.setattr(adapter_service, "available", lambda s, name: name in {"pi", "opencode"})
    task = _task(session)
    choice = adapter_service.choose_harness(session, task, model="kimi-k3.1-pro")
    assert choice.adapter == "pi" and "kimi-k3.1-pro" in choice.reason
    assert adapter_service.route_for(adapter_service.DEFAULT_HARNESS_ROUTES, None, "kimi-k3") == [
        "pi",
        "opencode",
        "aider",
    ]


def test_harness_routing_setting_overrides_defaults(session, monkeypatch):
    from orchestrator.services import settings_service

    monkeypatch.setattr(adapter_service, "available", lambda s, name: True)
    settings_service.update(
        session, {"harness_routing": {"qwen3.8": ["opencode"], "worker-glm": ["aider"]}}
    )
    assert adapter_service.choose_harness(session, _task(session)).adapter == "opencode"
    glm = _task(session, assigned_provider_profile="worker-glm")
    assert adapter_service.choose_harness(session, glm).adapter == "aider"
    import pytest

    with pytest.raises(ValueError):
        settings_service.update(session, {"harness_routing": {"qwen": "pi"}})


def test_measured_efficiency_reorders_route(session, monkeypatch):
    from orchestrator.core.models import Run

    monkeypatch.setattr(adapter_service, "available", lambda s, name: name in {"pi", "qwen_code"})
    for harness, tokens in (("pi", 90_000), ("qwen_code", 30_000)):
        for i in range(3):
            t = _task(session)
            t.status = "accepted"
            session.add(t)
            session.add(
                Run(
                    short_id=f"R-{harness}-{i}",
                    task_id=t.id,
                    harness_adapter=harness,
                    usage={
                        "source": "harness",
                        "model": "qwen3.8-flash",
                        "input_tokens": tokens,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 0,
                    },
                )
            )
    session.commit()
    choice = adapter_service.choose_harness(session, _task(session))
    assert choice.adapter == "qwen_code" and "per accepted task" in choice.reason


def test_unedited_legacy_adapter_templates_are_upgraded(session):
    row = adapter_service.get_row(session, "deepseek_harness")
    row.args_template_json = ["run", "--cwd", "{cwd}", "--prompt-file", "{prompt_file}"]
    row.capabilities_json = {"experimental": True}
    custom = adapter_service.get_row(session, "qwen_code")
    custom.args_template_json = ["--yolo", "-p", "{prompt}"]
    claude = adapter_service.get_row(session, "claude_cli")
    claude.args_template_json = claude.args_template_json[:-2]
    session.add(claude)
    zcode = adapter_service.get_row(session, "zcode")
    zcode.command = "zcode-cli"
    zcode.args_template_json = ["run", "--cwd", "{cwd}", "--prompt-file", "{prompt_file}"]
    session.add(row)
    session.add(custom)
    session.add(zcode)
    session.commit()
    adapter_service.seed_defaults(session)
    assert adapter_service.get_row(session, "deepseek_harness").args_template_json[:2] == [
        "--profile",
        "headless",
    ]
    assert adapter_service.get_row(session, "deepseek_harness").capabilities_json[
        "prompt_via_stdin"
    ]
    assert adapter_service.get_row(session, "qwen_code").args_template_json == [
        "--yolo",
        "-p",
        "{prompt}",
    ]
    assert adapter_service.get_row(session, "claude_cli").args_template_json[-2:] == [
        "--allowedTools",
        "Bash",
    ]
    upgraded = adapter_service.get_row(session, "zcode")
    assert upgraded.command == "zcode"
    assert upgraded.args_template_json == [
        "--cwd",
        "{cwd}",
        "--mode",
        "yolo",
        "--json",
        "--prompt",
        "{prompt}",
    ]


def test_explicit_assignment_wins(session):
    t = _task(
        session,
        tags=["deepseek"],
        assigned_harness_adapter="aider",
        assigned_provider_profile="worker-glm",
    )
    assert adapter_service.choose_harness(session, t).adapter == "aider"
    assert provider_service.choose_profile(session, t, "worker").profile.name == "worker-glm"
