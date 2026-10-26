from pathlib import Path

import pytest

from orchestrator.adapters.base import RunContext, TemplateError, render_args
from orchestrator.adapters.registry import ADAPTER_CLASSES, build_adapter, default_specs


def test_render_args_optional_and_unknown():
    vals = {"model": "", "prompt_file": "/p.md", "cwd": "/w"}
    assert render_args(["run", "?--model={model}", "--file", "{prompt_file}"], vals) == [
        "run",
        "--file",
        "/p.md",
    ]
    assert render_args(["?--model={model}"], {**vals, "model": "glm-5.3"}) == ["--model=glm-5.3"]
    with pytest.raises(TemplateError):
        render_args(["{nope}"], vals)


def test_all_required_adapters_exist_and_real_ones_are_experimental():
    assert set(ADAPTER_CLASSES) == {
        "mock",
        "opencode",
        "deepseek_harness",
        "zcode",
        "qwen_code",
        "pi",
        "aider",
        "claude_cli",
    }
    for kind, cls in ADAPTER_CLASSES.items():
        assert cls.experimental == (kind != "mock")
        for method in ("detect", "health", "build_command", "run", "cancel", "parse_result"):
            assert callable(getattr(cls, method))


def test_default_templates_render_without_shell(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the thing")
    ctx = RunContext(
        run_id=1,
        task_id=1,
        cwd=tmp_path,
        prompt_file=prompt,
        artifacts_dir=tmp_path,
        handoff_path=tmp_path / "h.json",
        branch="phase-1-x",
        phase="phase-1",
        model="qwen3.8-flash",
    )
    for spec in default_specs():
        argv = build_adapter(spec).build_command(ctx)
        assert argv[0] == spec.command and all(isinstance(a, str) for a in argv)
    qwen = next(s for s in default_specs() if s.kind == "qwen_code")
    assert build_adapter(qwen).build_command(ctx)[:1] == ["qwen"]
    assert "do the thing" in build_adapter(qwen).build_command(ctx)
    # Previews never inline the prompt text.
    assert "do the thing" not in " ".join(build_adapter(qwen).command_preview(ctx))


def test_command_env_override(monkeypatch):
    monkeypatch.setenv("QWEN_CODE_COMMAND", "/opt/qwen/bin/qwen")
    qwen = next(s for s in default_specs() if s.kind == "qwen_code")
    assert qwen.command == "/opt/qwen/bin/qwen"


def test_claude_cli_disabled_by_default():
    assert not next(s for s in default_specs() if s.kind == "claude_cli").enabled


def test_oversized_inline_prompt_rejected(tmp_path):
    prompt = tmp_path / "p.md"
    prompt.write_text("x" * 200_000)
    spec = next(s for s in default_specs() if s.kind == "opencode")
    ctx = RunContext(
        run_id=1,
        task_id=1,
        cwd=Path(tmp_path),
        prompt_file=prompt,
        artifacts_dir=tmp_path,
        handoff_path=tmp_path / "h",
        branch="b",
        phase="p",
    )
    with pytest.raises(TemplateError):
        build_adapter(spec).build_command(ctx)
