"""Mock harness: a real subprocess (this module's __main__) that makes a deterministic
change, commits it on the phase branch, and writes a handoff.

Tags: `mock-fail` -> exit 1, `mock-timeout` -> hang until killed (adapter timeout is
clamped to 2s), `mock-blocked` -> BLOCKED handoff and no change.

T-10 tags reproduce what a killed run really leaves in the worktree — offline, no live service.
Every tag ending in `-handoff` writes the submission BEFORE the run ends badly, which is exactly
the ordering the salvage path exists for:

  `mock-timeout-handoff`        commit + valid handoff, then hang until the timeout kills it
  `mock-cancel-handoff`         commit + valid handoff, then hang until the cancel marker hits
  `mock-fail-handoff`           commit + valid handoff, then exit 1
  `mock-timeout-bad-handoff`    commit, then write a TRUNCATED handoff and hang
  `mock-timeout-promise-handoff`  write an UNCOMMITTED file, claim it in the handoff, then hang
                                (the run-13 shape: a promise the branch does not contain)
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

# Tags this adapter understands (everything else starting with `mock-` is a routing tag).
KNOWN_TAGS = {
    "mock-fail",
    "mock-timeout",
    "mock-blocked",
    "mock-timeout-handoff",
    "mock-cancel-handoff",
    "mock-fail-handoff",
    "mock-timeout-bad-handoff",
    "mock-timeout-promise-handoff",
}
# Tags that hang: the adapter timeout is clamped so a test waits ~2s, not an hour.
HANG_TAGS = {
    "mock-timeout",
    "mock-timeout-handoff",
    "mock-cancel-handoff",
    "mock-timeout-bad-handoff",
    "mock-timeout-promise-handoff",
}


class MockHarnessAdapter(BaseHarnessAdapter):
    kind = "mock"
    experimental = False
    default_command = sys.executable
    default_args = [
        "-m",
        "orchestrator.adapters.mock_harness",
        "--cwd",
        "{cwd}",
        "--prompt-file",
        "{prompt_file}",
        "--handoff",
        "{handoff_path}",
        "--branch",
        "{branch}",
        "--run-id",
        "{run_id}",
    ]
    default_capabilities = {"commits": True, "writes_handoff": True, "offline": True}
    description = "Offline deterministic harness for demo and tests."
    verify_hint = "orchestrator demo"

    def __init__(self, spec: AdapterSpec):
        super().__init__(spec)

    def build_command(self, ctx: RunContext) -> list[str]:
        argv = super().build_command(ctx)
        for tag in sorted(t for t in ctx.tags if t in KNOWN_TAGS):
            argv += ["--tag", tag]
        return argv

    async def run(self, ctx: RunContext, runner):
        if any(t in HANG_TAGS for t in ctx.tags):
            ctx.timeout_seconds = min(ctx.timeout_seconds, MOCK_TIMEOUT_SECONDS)
        return await super().run(ctx, runner)

    def detect(self) -> bool:
        return True


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _commit_change(cwd: Path, run_id: str, phase: str) -> None:
    target = cwd / "MOCK_CHANGE.md"
    existing = target.read_text() if target.exists() else "# Mock changes\n"
    target.write_text(existing + f"- run {run_id}: deterministic mock change\n")
    _git(cwd, "add", "MOCK_CHANGE.md")
    _git(cwd, "commit", "-q", "-m", f"feat({phase}): mock change for run {run_id}")
    print("[mock-harness] committed MOCK_CHANGE.md", flush=True)


def _handoff(phase: str, branch: str, status: str = "READY_FOR_REVIEW") -> dict:
    return {
        "phase": phase,
        "branch": branch,
        "status": status,
        "summary": "",
        "files_changed": [],
        "tests": {"command": "", "passed": 0, "failed": 0, "skipped": 0, "log_path": ""},
        "dod_self_check": [],
        "new_dependencies": [],
        "assumptions": [],
        "questions": [],
        "blockers": [],
        "contracts_touched": [],
        "memory_suggestions": [],
    }


def _write(path: Path, payload: dict | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    path.write_text(text)
    print(f"[mock-harness] wrote handoff ({len(text)} bytes)", flush=True)


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
    phase = a.branch
    prompt = Path(a.prompt_file).read_text()
    print(f"[mock-harness] run {a.run_id} on {a.branch}; prompt {len(prompt)} chars", flush=True)

    if "mock-timeout" in tags:
        # The plain timeout: killed before it wrote anything, so there is nothing to salvage.
        print("[mock-harness] simulating a hang", flush=True)
        time.sleep(3600)
        return 0

    if "mock-fail" in tags:
        print("[mock-harness] simulating failure", file=sys.stderr, flush=True)
        return 1

    handoff = _handoff(phase, a.branch)
    if "mock-blocked" in tags:
        handoff |= {
            "status": "BLOCKED",
            "summary": "Blocked: the mock phase requires a frozen contract change.",
            "blockers": ["frozen contract: handoff.json schema would need a new field"],
            "questions": ["Approve a contract-change request, or descope the DoD item?"],
        }
        _write(Path(a.handoff), handoff)
        return 0

    if "mock-timeout-promise-handoff" in tags:
        # A promise, not delivered work: the file exists only in the worktree and the handoff
        # claims it. Measured on run 13, where this shape arrived from a cancelled process.
        (cwd / "PROMISE_ONLY.md").write_text("# written but never committed by the Worker\n")
        handoff |= {
            "summary": "Added PROMISE_ONLY.md (it is not committed).",
            "files_changed": ["PROMISE_ONLY.md"],
            "dod_self_check": [{"item": "PROMISE_ONLY.md updated", "met": True, "evidence": ""}],
        }
        _write(Path(a.handoff), handoff)
        print("[mock-harness] hung with an uncommitted claim", flush=True)
        time.sleep(3600)
        return 0

    if "mock-timeout-bad-handoff" in tags:
        _commit_change(cwd, a.run_id, phase)
        _write(Path(a.handoff), '{"phase": "p", "branch": "b", "summary": "trunca')
        print("[mock-harness] hung with a truncated handoff", flush=True)
        time.sleep(3600)
        return 0

    _commit_change(cwd, a.run_id, phase)
    handoff |= {
        "summary": "Appended a deterministic line to MOCK_CHANGE.md.",
        "files_changed": ["MOCK_CHANGE.md"],
        "dod_self_check": [
            {
                "item": "MOCK_CHANGE.md updated",
                "met": True,
                "evidence": "committed on phase branch",
            }
        ],
        "assumptions": ["Tests are run by the orchestrator verification step."],
        "memory_suggestions": [
            {
                "category": "project",
                "scope": "conventions",
                "content": "Mock changes are appended to MOCK_CHANGE.md, one line per run.",
                "tags": ["mock"],
            },
            {
                "category": "role",
                "scope": "worker",
                "content": "Commit on the phase branch with feat(phase-N): messages.",
                "tags": ["git"],
            },
        ],
    }
    _write(Path(a.handoff), handoff)

    if "mock-timeout-handoff" in tags or "mock-cancel-handoff" in tags:
        why = "cancel" if "mock-cancel-handoff" in tags else "timeout"
        print(f"[mock-harness] handoff written, hanging until {why}", flush=True)
        time.sleep(3600)
        return 0
    if "mock-fail-handoff" in tags:
        print("[mock-harness] handoff written, exiting 1", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
