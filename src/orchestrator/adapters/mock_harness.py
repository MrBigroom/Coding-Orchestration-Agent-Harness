"""Mock harness: a real subprocess (this module's __main__) that makes a deterministic
change, commits it on the phase branch, and writes a handoff.

Tags: `mock-fail` -> exit 1, `mock-timeout` -> hang until killed (adapter timeout is
clamped to 2s), `mock-blocked` -> BLOCKED handoff and no change.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from orchestrator.adapters.base import AdapterSpec, BaseHarnessAdapter, RunContext

MOCK_TIMEOUT_SECONDS = 2


class MockHarnessAdapter(BaseHarnessAdapter):
    kind = "mock"
    experimental = False
    default_command = sys.executable
    default_args = [
        "-m", "orchestrator.adapters.mock_harness",
        "--cwd", "{cwd}", "--prompt-file", "{prompt_file}", "--handoff", "{handoff_path}",
        "--branch", "{branch}", "--run-id", "{run_id}",
    ]
    default_capabilities = {"commits": True, "writes_handoff": True, "offline": True}
    description = "Offline deterministic harness for demo and tests."
    verify_hint = "orchestrator demo"

    def __init__(self, spec: AdapterSpec):
        super().__init__(spec)

    def build_command(self, ctx: RunContext) -> list[str]:
        argv = super().build_command(ctx)
        for tag in ("mock-fail", "mock-timeout", "mock-blocked"):
            if tag in ctx.tags:
                argv += ["--tag", tag]
        return argv

    async def run(self, ctx: RunContext, runner):
        if "mock-timeout" in ctx.tags:
            ctx.timeout_seconds = min(ctx.timeout_seconds, MOCK_TIMEOUT_SECONDS)
        return await super().run(ctx, runner)

    def detect(self) -> bool:
        return True


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--cwd", required=True)
    p.add_argument("--prompt-file", required=True)
    p.add_argument("--handoff", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--tag", action="append", default=[])
    a = p.parse_args(argv)
    cwd = Path(a.cwd)
    tags = set(a.tag)
    prompt = Path(a.prompt_file).read_text()
    print(f"[mock-harness] run {a.run_id} on {a.branch}; prompt {len(prompt)} chars", flush=True)

    if "mock-timeout" in tags:
        print("[mock-harness] simulating a hang", flush=True)
        time.sleep(3600)
        return 0
    if "mock-fail" in tags:
        print("[mock-harness] simulating failure", file=sys.stderr, flush=True)
        return 1

    phase = a.branch
    handoff: dict = {
        "phase": phase, "branch": a.branch, "summary": "", "files_changed": [],
        "tests": {"command": "", "passed": 0, "failed": 0, "skipped": 0, "log_path": ""},
        "dod_self_check": [], "new_dependencies": [], "assumptions": [], "questions": [],
        "blockers": [], "contracts_touched": [], "memory_suggestions": [],
    }
    if "mock-blocked" in tags:
        handoff |= {
            "status": "BLOCKED",
            "summary": "Blocked: the mock phase requires a frozen contract change.",
            "blockers": ["frozen contract: handoff.json schema would need a new field"],
            "questions": ["Approve a contract-change request, or descope the DoD item?"],
        }
    else:
        target = cwd / "MOCK_CHANGE.md"
        existing = target.read_text() if target.exists() else "# Mock changes\n"
        target.write_text(existing + f"- run {a.run_id}: deterministic mock change\n")
        _git(cwd, "add", "MOCK_CHANGE.md")
        _git(cwd, "commit", "-q", "-m", f"feat({phase}): mock change for run {a.run_id}")
        print("[mock-harness] committed MOCK_CHANGE.md", flush=True)
        handoff |= {
            "status": "READY_FOR_REVIEW",
            "summary": "Appended a deterministic line to MOCK_CHANGE.md.",
            "files_changed": ["MOCK_CHANGE.md"],
            "dod_self_check": [{"item": "MOCK_CHANGE.md updated", "met": True,
                                "evidence": "committed on phase branch"}],
            "assumptions": ["Tests are run by the orchestrator verification step."],
            "memory_suggestions": [
                {"category": "project", "scope": "conventions",
                 "content": "Mock changes are appended to MOCK_CHANGE.md, one line per run.",
                 "tags": ["mock"]},
                {"category": "role", "scope": "worker",
                 "content": "Commit on the phase branch with feat(phase-N): messages.",
                 "tags": ["git"]},
            ],
        }
    hp = Path(a.handoff)
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(json.dumps(handoff, indent=2))
    print(f"[mock-harness] wrote handoff ({handoff['status']})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
