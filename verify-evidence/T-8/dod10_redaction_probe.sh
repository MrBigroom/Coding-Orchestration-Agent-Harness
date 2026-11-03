#!/usr/bin/env bash
# T-8 DoD 10 evidence: the sk- credential patterns are anchored, and they were not before.
# Runs the SAME probes against the OLD pattern set (reconstructed here, byte-for-byte from
# utils/redaction.py at 14a221c) and against the shipped `redact()` at this HEAD, so the
# regression and the preserved behaviour are both visible in one output. Offline, no network.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY=/run/media/mrbigroom/Backup2TB/Projects/Coding-Orchestration-Agent-Harness/.venv/bin/python
cd "$ROOT"

PYTHONPATH="$ROOT/src" "$PY" - <<'PY'
import re
from orchestrator.utils.redaction import redact

OLD = [re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}")]


def old_redact(text):
    out = text
    for p in OLD:
        out = p.sub("[REDACTED]", out)
    return out


OPERATIONAL = [
    "git -C /repo merge --no-ff task-8-recovery-commits-after-a-timeout-commit-half-fin",
    "task-6-verification-commands-cannot-run-in-a-worktree-n",
    'findings_json={"branch": "task-8-recovery-commits-after-a-timeout-commit-half-fin"}',
    "phase-3-risk-assessment-and-masked-payloads",
    "src/orchestrator/services/task_service.py",
]
CREDENTIALS = [
    "sk-ant-api03-abcdefghijklmnopqrstuvwx",
    "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    "sk-abcdefghijklmnopqrstuvwxyz0123456789",
]

print("== names that must survive (old pattern -> new pattern) ==")
for t in OPERATIONAL:
    o, n = old_redact(t), redact(t)
    print(f"  old_damaged={o != t!s:<5} new_damaged={n != t!s:<5}  {n}")

print("== credentials that must still redact ==")
for t in CREDENTIALS:
    n = redact(t)
    print(f"  redacted={t not in n!s:<5}  {n[:60]}")

print("== real build-log line shape (merge_candidate, id=86 was destroyed by the old pattern) ==")
line = (
    "merge gate disabled; run: git -C /repo checkout main && git -C /repo merge --no-ff "
    "task-8-recovery-commits-after-a-timeout-commit-half-fin"
)
print("  old:", old_redact(line)[:70], "...")
print("  new:", redact(line))
assert redact(line) == line
PY
