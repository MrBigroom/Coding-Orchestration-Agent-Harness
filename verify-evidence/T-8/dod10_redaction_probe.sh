#!/usr/bin/env bash
# T-8 DoD 10 evidence: the sk- credential patterns are anchored, and they were not before.
# Compares three pattern sets over the same strings —
#   OLD   : the unanchored patterns as shipped at 14a221c (destroyed branch/task names)
#   BOUND : the naive `\\b` word boundary (fixes the names but MISSES `TOKEN_sk-proj-…`,
#           because `_` is a word character — a weakening the DoD forbids)
#   SHIPPED: the `(?<![A-Za-z0-9])` lookbehind actually in utils/redaction.py
# Offline, deterministic, no network.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY=/run/media/mrbigroom/Backup2TB/Projects/Coding-Orchestration-Agent-Harness/.venv/bin/python
cd "$ROOT" || exit 1

PYTHONPATH="$ROOT/src" "$PY" - <<'PY'
import re
from orchestrator.utils.redaction import redact

OLD = [r"sk-ant-[A-Za-z0-9_\-]{10,}", r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"]
BOUND = [r"\bsk-ant-[A-Za-z0-9_\-]{10,}", r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"]
SHIPPED = [r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_\-]{10,}",
           r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"]


def with_patterns(pats, text):
    out = text
    for p in pats:
        out = re.sub(p, "[REDACTED]", out)
    return out


NAMES = [
    "git -C /repo merge --no-ff task-8-recovery-commits-after-a-timeout-commit-half-fin",
    "task-6-verification-commands-cannot-run-in-a-worktree-n",
    'findings_json={"branch": "task-8-recovery-commits-after-a-timeout-commit-half-fin"}',
    "phase-3-risk-assessment-and-masked-payloads",
    "src/orchestrator/services/task_service.py",
]
KEYS = [
    "sk-ant-api03-abcdefghijklmnopqrstuvwx",
    "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    "sk-abcdefghijklmnopqrstuvwxyz0123456789",
]

print("== names that MUST survive (damaged?  OLD / BOUND / SHIPPED) ==")
for t in NAMES:
    print("  ", *[f"{label}={with_patterns(p, t) != t!s:<5}" for label, p in
                  (("OLD", OLD), ("BOUND", BOUND), ("SHIPPED", SHIPPED))], t[:58])

print("== keys that MUST redact, by position (present after redaction?  OLD / BOUND / SHIPPED) ==")
POSITIONS = {
    "bare": lambda k: k,
    "after =": lambda k: f"OPENAI_KEY={k}",
    "quoted": lambda k: f'"token": "{k}"',
    "hyphen-joined": lambda k: f"prefix-{k}",
    "underscore-joined": lambda k: f"OPENAI_TOKEN_{k}",
}
for k in KEYS:
    for label, shape in POSITIONS.items():
        text = shape(k)
        res = {n: (k in with_patterns(p, text)) for n, p in (("OLD", OLD), ("BOUND", BOUND),
                                                             ("SHIPPED", SHIPPED))}
        flag = "  <-- `\\b` would leak this" if res["BOUND"] and not res["SHIPPED"] else ""
        print(f"  {label:<19} {k[:18]:<20} leaked_by OLD={res['OLD']!s:<5} "
              f"BOUND={res['BOUND']!s:<5} SHIPPED={res['SHIPPED']!s:<5}{flag}")

print("== the shipped redact() on the real build-log line (build_log_entries.id=86 shape) ==")
line = ("merge gate disabled; run: git -C /repo checkout main && git -C /repo merge --no-ff "
        "task-8-recovery-commits-after-a-timeout-commit-half-fin")
print("  OLD    :", with_patterns(OLD, line)[:78], "…[branch name destroyed]")
print("  SHIPPED:", redact(line))
assert redact(line) == line
assert "OPENAI_TOKEN_" + KEYS[1] != redact("OPENAI_TOKEN_" + KEYS[1])
print("\n== other credential families, unchanged by this task (historical \\b anchors) ==")
for t in ["ghp_abcdefghijklmnopqrstuvwxyz1234", "AKIAIOSFODNN7EXAMPLE", "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p67",
          "glpat-abcdefghijklmnopqrstuvw", "xoxb-123456789012-abcdefghijklmnopqrst",
          "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrstuvwxyz123456"]:
    print(f"  redacted={t not in redact(t)!s:<5} bare;  underscore-joined redacted="
          f"{t not in redact('TOKEN_' + t)!s:<5} (pre-existing gap, pinned by a test, not widened here)")
PY
