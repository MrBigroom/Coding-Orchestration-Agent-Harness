"""Pi exits 0 when its last response is cut off, so the adapter reports the failure."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from orchestrator.adapters.base import AdapterResult, RunContext
from orchestrator.adapters.registry import build_adapter, default_specs


def _log(stop_reason: str) -> str:
    message = {"role": "assistant", "stopReason": stop_reason}
    events = [
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "toolUse"}},
        {"type": "message_end", "message": {"role": "toolResult"}},
        {"type": "message_end", "message": message},
        {"type": "agent_end"},
    ]
    return "".join(json.dumps(e) + "\n" for e in events)


def _run(tmp_path: Path, stop_reason: str, exit_code: int = 0) -> AdapterResult:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x")
    log = tmp_path / "stdout.log"
    log.write_text(_log(stop_reason))
    ctx = RunContext(
        run_id=1,
        task_id=1,
        cwd=tmp_path,
        prompt_file=prompt,
        artifacts_dir=tmp_path,
        handoff_path=tmp_path / "handoff.json",
        branch="b",
        phase="p",
        model="deepseek-v4.1-flash",
    )

    async def runner(**_kwargs):
        return AdapterResult(exit_code=exit_code, stdout_path=log)

    spec = next(s for s in default_specs() if s.kind == "pi")
    return asyncio.run(build_adapter(spec).run(ctx, runner))


@pytest.mark.parametrize("reason", ["length", "error"])
def test_cut_off_run_without_handoff_fails(tmp_path, reason):
    assert reason in (_run(tmp_path, reason).error or "")


def test_normal_stop_is_not_an_error(tmp_path):
    assert _run(tmp_path, "stop").error is None


def test_cut_off_run_with_handoff_is_kept(tmp_path):
    (tmp_path / "handoff.json").write_text("{}")
    assert _run(tmp_path, "length").error is None


def test_non_zero_exit_keeps_its_own_error(tmp_path):
    assert _run(tmp_path, "length", exit_code=1).error is None
