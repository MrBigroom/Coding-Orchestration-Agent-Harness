"""Stdout captured through a file instead of a pipe (Qwen Code)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from orchestrator.adapters.base import AdapterResult, RunContext
from orchestrator.adapters.registry import build_adapter, default_specs
from orchestrator.services.process_service import run_process

SECRET = "sk-" + "a" * 30


def _run(tmp_path: Path, code: str, **kwargs) -> AdapterResult:
    return asyncio.run(
        run_process(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env_names=[],
            artifacts_dir=tmp_path / "art",
            run_id=1,
            timeout_seconds=30,
            **kwargs,
        )
    )


def test_piped_line_over_asyncio_default_limit_is_logged(tmp_path):
    code = "print('x ' * 100_000); print('done')"
    result = _run(tmp_path, code)
    assert result.exit_code == 0
    assert result.stdout_path.read_text().splitlines()[1:] == ["done"]


def test_stdout_file_logs_long_line_redacted(tmp_path):
    code = f"print({SECRET!r} + ' x' * 100_000); print('done')"
    result = _run(tmp_path, code, stdout_file=tmp_path / "raw")
    lines = result.stdout_path.read_text().splitlines()
    assert result.exit_code == 0
    assert lines == ["[REDACTED]" + " x" * 100_000, "done"]


def test_stdout_file_keeps_stderr_on_a_pipe(tmp_path):
    code = "import sys; print('out'); print('err', file=sys.stderr)"
    result = _run(tmp_path, code, stdout_file=tmp_path / "raw")
    assert result.stdout_path.read_text() == "out\n"
    assert result.stderr_path.read_text() == "err\n"


def test_stdout_file_is_logged_after_a_timeout(tmp_path):
    code = "import time; print('partial', flush=True); time.sleep(30)"
    (tmp_path / "art").mkdir()
    result = asyncio.run(
        run_process(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env_names=[],
            artifacts_dir=tmp_path / "art",
            run_id=1,
            timeout_seconds=1,
            stdout_file=tmp_path / "raw",
        )
    )
    assert result.timed_out and result.stdout_path.read_text() == "partial\n"


def test_failed_start_with_stdout_file(tmp_path):
    result = asyncio.run(
        run_process(
            ["/nonexistent/harness"],
            cwd=tmp_path,
            env_names=[],
            artifacts_dir=tmp_path / "art",
            run_id=1,
            timeout_seconds=5,
            stdout_file=tmp_path / "raw",
        )
    )
    assert result.exit_code is None and "failed to start" in (result.error or "")


def test_only_qwen_code_captures_stdout_to_a_file(tmp_path):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x")
    ctx = RunContext(
        run_id=1,
        task_id=1,
        cwd=tmp_path,
        prompt_file=prompt,
        artifacts_dir=tmp_path,
        handoff_path=tmp_path / "handoff.json",
        branch="b",
        phase="p",
    )
    seen: dict[str, dict] = {}

    async def runner(**kwargs):
        seen[kwargs["argv"][0]] = kwargs
        return AdapterResult(exit_code=0)

    for spec in default_specs():
        if spec.kind in {"qwen_code", "pi"}:
            asyncio.run(build_adapter(spec).run(ctx, runner))
    assert seen["qwen"]["stdout_file"].name == "stdout.raw"
    assert "stdout_file" not in seen["pi"]
