#!/usr/bin/env python3
"""Print the code behind each T-10 DoD claim, with file:line, from THIS checkout.

The review harness truncates large diffs, so a handoff that says "see run_service.py:461"
cannot be checked from the supplied diff alone. This script extracts the named functions and the
small number of literal blocks the DoD items point at, stamped with the HEAD it ran at. Output is
committed as code_map.txt next to this script.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FUNCTIONS = [
    ("src/orchestrator/services/run_service.py", "handoff_snapshot"),
    ("src/orchestrator/services/run_service.py", "read_handoff_file"),
    ("src/orchestrator/services/run_service.py", "delivery_evidence"),
    ("src/orchestrator/services/run_service.py", "try_salvage_handoff"),
    ("src/orchestrator/services/run_service.py", "archive_before_round_start"),
    ("src/orchestrator/services/run_service.py", "_gate_salvaged"),
    ("src/orchestrator/services/run_service.py", "_execute"),
    ("src/orchestrator/services/supervisor_service.py", "verify"),
    ("src/orchestrator/services/supervisor_service.py", "after_run"),
    ("src/orchestrator/services/review_service.py", "delivery_evidence_section"),
    ("src/orchestrator/services/review_service.py", "architect_review"),
    ("src/orchestrator/services/artifact_service.py", "render_delivery_evidence_md"),
    ("src/orchestrator/services/artifact_service.py", "handoff_delivery_evidence"),
    ("src/orchestrator/adapters/mock_harness.py", "run"),
    ("src/orchestrator/adapters/mock_harness.py", "main"),
]

GREPS = [
    ("T-10 constants", "src/orchestrator/services/run_service.py", "SALVAGE_MODE_"),
    ("T-10 handoff status", "src/orchestrator/services/artifact_service.py", "HANDOFF_STATUS_SALVAGED"),
    ("T-10 arrival", "src/orchestrator/services/artifact_service.py", "HANDOFF_ARRIVAL_"),
    ("T-10 claim status", "src/orchestrator/services/artifact_service.py", "CLAIM_"),
    ("T-10 check name", "src/orchestrator/services/supervisor_service.py", "SALVAGE_CHECK"),
    ("T-10 prompt section", "src/orchestrator/services/prompt_builder.py", "delivery_evidence"),
    ("T-10 cancel clamp", "src/orchestrator/adapters/mock_harness.py", "CANCEL"),
    ("T-10 docs section", "docs/build-loop.md", "Salvaging a handoff from a killed run"),
    ("T-10 docs lifecycle", "docs/build-loop.md", "SALVAGED is not"),
    ("T-10 docs event", "docs/build-loop.md", "handoff_salvaged"),
]

TESTS = [
    "test_timeout_with_a_valid_handoff_is_salvaged_and_reaches_the_gate",
    "test_cancel_with_a_valid_handoff_is_salvaged",
    "test_non_zero_exit_with_a_valid_handoff_is_salvaged",
    "test_timeout_with_no_handoff_is_exactly_todays_behaviour",
    "test_timeout_with_a_truncated_handoff_falls_back_and_logs_the_parse_failure",
    "test_a_pre_existing_submission_is_archived_before_the_round_start_unlink",
    "test_a_retry_after_a_salvaged_round_does_not_lose_the_archived_copy",
    "test_a_stale_leftover_is_not_salvaged_as_the_new_rounds_submission",
    "test_a_salvaged_promise_is_flagged_in_the_evidence_not_turned_into_a_new_gate",
    "test_the_architect_prompt_for_a_salvaged_run_carries_the_delivery_evidence",
]


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()


def block(path: str, symbol: str) -> str:
    text = (ROOT / path).read_text()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
            lines = text.splitlines()
            return (
                f"--- {path}:{node.lineno}-{node.end_lineno} def {symbol}\n"
                + "\n".join(lines[node.lineno - 1 : node.end_lineno])
            )
    return f"--- {path}: SYMBOL NOT FOUND: {symbol}"


def main() -> None:
    head = git("rev-parse", "HEAD")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    merge_base = git("merge-base", "HEAD", "main")
    dirty = len(git("status", "--porcelain", "-uall").splitlines())
    print(f"# HEAD={head} branch={branch} merge-base={merge_base} dirty-entries={dirty}")
    print("# command: python3 verify-evidence/T-10/collect_code_map.py")
    print()
    print("## Diffstat versus the merge-base")
    print(git("diff", "--stat", f"{merge_base}..HEAD"))
    print()
    print("## Named functions (extracted by AST from this checkout)")
    for path, symbol in FUNCTIONS:
        print(block(path, symbol))
        print()
    print("## Literal anchors")
    for label, path, needle in GREPS:
        print(f"--- {label}: {path} matching {needle!r}")
        out = subprocess.run(
            ["grep", "-n", needle, path],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.rstrip()
        print(out or "(no match)")
        print()
    print("## T-10 test functions (file:line)")
    for name in TESTS:
        out = subprocess.run(
            ["grep", "-rn", f"def {name}", "tests"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.rstrip()
        print(out or f"MISSING: {name}")


if __name__ == "__main__":
    main()