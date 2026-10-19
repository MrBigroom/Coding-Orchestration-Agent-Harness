from orchestrator.core.schemas import MemoryCreate, TaskCreate
from orchestrator.providers.base import PromptSection
from orchestrator.services import memory_service, prompt_builder, task_service
from orchestrator.services.prompt_builder import SECTION_ORDER, DynamicState


def test_sections_follow_static_first_order(session):
    memory_service.create(
        session,
        MemoryCreate(
            category="role",
            scope="worker",
            role="worker",
            content="Keep diffs small.",
            approved=True,
        ),
    )
    task = task_service.create(session, TaskCreate(title="Phase", definition_of_done=["A", "B"]))
    built = prompt_builder.build_prompt(
        session,
        "worker",
        task,
        None,
        DynamicState(diff="diff --git a/x b/x\n+1", test_summary="passed: 1", feedback="fix X"),
    )
    names = [s.name for s in built.sections]
    assert names == SECTION_ORDER
    statics = [s.static for s in built.sections]
    # Once dynamic starts, nothing static follows.
    assert statics == sorted(statics, reverse=True)
    assert built.sections[0].content.startswith("You are the Worker")
    assert "Keep diffs small." in built.sections[2].content
    assert "fix X" in built.sections[-1].content and "1. A" in built.sections[5].content
    assert prompt_builder.preview(built.sections)["ok"] is True


def test_dynamic_state_never_leaks_into_static_sections(session):
    task = task_service.create(session, TaskCreate(title="P"))
    built = prompt_builder.build_prompt(
        session, "architect", task, None, DynamicState(diff="UNIQUE_DIFF_MARKER")
    )
    assert all("UNIQUE_DIFF_MARKER" not in s.content for s in built.sections if s.static)


def test_preview_detects_dynamic_before_static():
    sections = [
        PromptSection("system", "role", static=True),
        PromptSection("dynamic_state", "latest diff", static=False),
        PromptSection("project_memory", "conventions", static=True),
    ]
    report = prompt_builder.preview(sections)
    assert report["ok"] is False
    assert any(
        "dynamic section 'dynamic_state' appears before static section 'project_memory'" in w
        for w in report["warnings"]
    )


def test_preview_flags_volatile_values_in_static_prefix():
    report = prompt_builder.preview(
        [PromptSection("system", "now: 2026-10-19 08:00:01", static=True)]
    )
    assert any("timestamp" in w for w in report["warnings"])


def test_secrets_redacted_and_sensitive_diffs_stripped(session):
    task = task_service.create(session, TaskCreate(title="P"))
    diff = (
        "diff --git a/.env b/.env\n+ANTHROPIC_API_KEY=sk-ant-api03-aaaaaaaaaaaaaaaaaaaa\n"
        "diff --git a/app.py b/app.py\n+x = 1\n"
    )
    built = prompt_builder.build_prompt(session, "architect", task, None, DynamicState(diff=diff))
    text = built.static_text() + built.dynamic_text()
    assert "sk-ant-api03" not in text and "x = 1" in text


def test_budget_truncates_dynamic_only(session, monkeypatch):
    from orchestrator.core.config import get_settings

    monkeypatch.setattr(get_settings(), "max_prompt_context_tokens", 3000)
    task = task_service.create(session, TaskCreate(title="P"))
    built = prompt_builder.build_prompt(
        session, "worker", task, None, DynamicState(handoff="x" * 200_000)
    )
    system = built.sections[0].content
    assert system.startswith("You are the Worker")  # static untouched
    assert "structured summary" in built.sections[-1].content
